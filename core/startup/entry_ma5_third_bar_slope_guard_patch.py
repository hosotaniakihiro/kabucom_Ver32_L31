# ============================================================
# File   : core/startup/entry_ma5_third_bar_slope_guard_patch.py
# Version: V3-STRONG-SCORE-FAILOPEN
# ------------------------------------------------------------
# BUY:
#   3分足・5分足で価格がMA5を超えて1本目/2本目では入らず、
#   3本目でMA5傾きがプラスの時だけ許可する。
# SELL:
#   3分足・5分足で価格がMA5を下抜けて1本目/2本目では入らず、
#   3本目でMA5傾きがマイナスの時だけ許可する。
#
# V3:
#   - entry_controller の候補itemでは source=RANKING が欠落する場合があり、
#     V2 の RANKING_STRONG_FAILOPEN が発動せず、score 90 の候補まで
#     ma5_third_bar_slope_ng で全落ちしていた。
#   - strong score 候補は source が取れなくても、このガード単独では落とさない。
#   - 低スコア候補は従来通り MA5 方向確認を維持。
# ============================================================
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)
_INSTALLED = False
_ORIG_BUILD = None


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(v))
    except Exception:
        return int(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _tfs() -> list[int]:
    raw = os.getenv("ENTRY_MA5_THIRD_BAR_REQUIRED_TFS", "3,5")
    out: list[int] = []
    for x in str(raw).replace(";", ",").split(","):
        try:
            n = int(float(x.strip()))
            if n in (3, 5) and n not in out:
                out.append(n)
        except Exception:
            pass
    return out or [3, 5]


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return float(default)
        s = str(v).strip()
        if s == "" or s.lower() in {"none", "nan", "nat"}:
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _row_sources(item: dict[str, Any]):
    try:
        for src in (item, item.get("entry_row"), item.get("entry"), item.get("row"), item.get("raw"), item.get("_raw")):
            if isinstance(src, dict):
                yield src
    except Exception:
        return


def _symbol_from_item(item: dict[str, Any]) -> str:
    try:
        for src in _row_sources(item):
            s = str(src.get("symbol") or "").strip()
            if s.endswith(".0"):
                s = s[:-2]
            if s:
                return s
    except Exception:
        pass
    return ""


def _side_from_item(item: dict[str, Any]) -> str:
    try:
        for src in _row_sources(item):
            s = str(src.get("side") or src.get("entry_decision") or src.get("resolved_side") or "").strip().upper()
            if s in {"BUY", "SELL"}:
                return s
    except Exception:
        pass
    return ""


def _source_from_item(item: dict[str, Any]) -> str:
    keys = ("source", "entry_source", "entry_type", "pipeline_source", "ranking_entry_mode", "ranking_source")
    try:
        vals = []
        for src in _row_sources(item):
            for k in keys:
                v = src.get(k)
                if v:
                    vals.append(str(v).strip().upper())
        return ",".join(vals)
    except Exception:
        pass
    return ""


def _score_from_item(item: dict[str, Any]) -> float:
    keys = (
        "priority",
        "pending_score",
        "score",
        "final_score",
        "display_score",
        "score_total",
        "total_score",
        "ranking_only_score",
        "ranking_strength",
        "snapshot_score",
    )
    try:
        vals = []
        for src in _row_sources(item):
            for k in keys:
                if k in src:
                    vals.append(abs(_safe_float(src.get(k), 0.0)))
        return max(vals, default=0.0)
    except Exception:
        return 0.0


def _strong_failopen_ok(item: dict[str, Any], diag: dict[str, Any]) -> bool:
    if not _env_bool("ENTRY_MA5_THIRD_BAR_STRONG_SCORE_FAILOPEN", True):
        return False

    source = _source_from_item(item)
    score = _score_from_item(item)
    min_score = _env_float(
        "ENTRY_MA5_THIRD_BAR_STRONG_FAILOPEN_MIN_SCORE",
        _env_float("ENTRY_MA5_THIRD_BAR_RANKING_FAILOPEN_MIN_SCORE", 75.0),
    )
    if score < min_score:
        return False

    # 明示的にRANKINGなら通す。sourceが欠落していても high score は通す。
    allow_unknown_source = _env_bool("ENTRY_MA5_THIRD_BAR_STRONG_FAILOPEN_ALLOW_UNKNOWN_SOURCE", True)
    if source and "RANKING" not in source and not _env_bool("ENTRY_MA5_THIRD_BAR_STRONG_FAILOPEN_ALL_SOURCES", False):
        return False
    if not source and not allow_unknown_source:
        return False

    max_bad_slope_abs = _env_float("ENTRY_MA5_THIRD_BAR_RANKING_MAX_BAD_SLOPE_ABS", 999999.0)
    try:
        bad_slopes = []
        for c in diag.get("checks", []) or []:
            if isinstance(c, dict) and c.get("ok") is False:
                bad_slopes.append(abs(_safe_float(c.get("ma5_slope"), 0.0)))
        if bad_slopes and max(bad_slopes) > max_bad_slope_abs:
            return False
    except Exception:
        pass

    logger.warning(
        "[ENTRY MA5 THIRD BAR GUARD] STRONG_SCORE_FAILOPEN symbol=%s side=%s source=%s score=%.3f min_score=%.3f diag=%s",
        diag.get("symbol"),
        diag.get("side"),
        source or "UNKNOWN",
        score,
        min_score,
        diag,
    )
    return True


def _ranking_strong_failopen_ok(item: dict[str, Any], diag: dict[str, Any]) -> bool:
    # backward-compatible env name
    if not _env_bool("ENTRY_MA5_THIRD_BAR_RANKING_STRONG_FAILOPEN", True):
        return False
    return _strong_failopen_ok(item, diag)


def _latest_rows_for_symbol(tf: int, symbol: str):
    try:
        import pandas as pd
        from global_state import global_data
        getter = getattr(global_data, "get_summary_history", None)
        df = getter(tf, source="push") if callable(getter) else None
        if df is None or not isinstance(df, pd.DataFrame) or df.empty or "symbol" not in df.columns:
            return None
        d = df.copy()
        s = d["symbol"].astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
        d = d[s == str(symbol).strip()]
        if d.empty:
            return None
        time_col = next((c for c in ("datetime", "end_time", "time", "start_time") if c in d.columns), None)
        if time_col:
            d["__dt"] = pd.to_datetime(d[time_col], errors="coerce")
            d = d.sort_values("__dt")
        return d.tail(max(3, _env_int("ENTRY_MA5_THIRD_BAR_MIN_BARS", 3)))
    except Exception:
        logger.exception("[ENTRY MA5 THIRD BAR GUARD] get rows failed tf=%s symbol=%s", tf, symbol)
        return None


def _check_tf(tf: int, symbol: str, side: str) -> tuple[bool | None, dict[str, Any]]:
    rows = _latest_rows_for_symbol(tf, symbol)
    if rows is None or getattr(rows, "empty", True):
        return None, {"tf": tf, "reason": "no_history"}
    min_bars = max(3, _env_int("ENTRY_MA5_THIRD_BAR_MIN_BARS", 3))
    if len(rows) < min_bars:
        return None, {"tf": tf, "reason": "not_enough_bars", "rows": len(rows), "need": min_bars}
    if "ma5" not in rows.columns:
        return None, {"tf": tf, "reason": "ma5_missing"}
    price_col = next((c for c in ("close", "close_price", "price", "current_price") if c in rows.columns), "close")
    last3 = rows.tail(3)
    closes = [_safe_float(x, 0.0) for x in list(last3[price_col])]
    ma5s = [_safe_float(x, 0.0) for x in list(last3["ma5"])]
    if any(x <= 0 for x in closes) or any(x <= 0 for x in ma5s):
        return None, {"tf": tf, "reason": "bad_close_or_ma5", "closes": closes, "ma5s": ma5s}
    slope = ma5s[-1] - ma5s[-2]
    if side == "BUY":
        ok = all(c > m for c, m in zip(closes, ma5s)) and slope > 0
    else:
        ok = all(c < m for c, m in zip(closes, ma5s)) and slope < 0
    return bool(ok), {"tf": tf, "side": side, "closes": [round(x, 4) for x in closes], "ma5s": [round(x, 4) for x in ma5s], "ma5_slope": round(float(slope), 6), "ok": bool(ok)}


def _passes_ma5_guard(item: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    symbol = _symbol_from_item(item)
    side = _side_from_item(item)
    if not symbol or side not in {"BUY", "SELL"}:
        return True, {"reason": "no_symbol_or_side", "symbol": symbol, "side": side}
    checks = []
    seen = 0
    for tf in _tfs():
        ok, diag = _check_tf(tf, symbol, side)
        checks.append(diag)
        if ok is None:
            continue
        seen += 1
        if not ok:
            out = {"symbol": symbol, "side": side, "checks": checks, "reason": "ma5_third_bar_slope_ng"}
            if _ranking_strong_failopen_ok(item, out):
                out["reason"] = "ma5_third_bar_strong_score_failopen"
                return True, out
            return False, out
    if seen <= 0:
        if _env_bool("ENTRY_MA5_THIRD_BAR_FAIL_OPEN", True):
            return True, {"symbol": symbol, "side": side, "checks": checks, "reason": "no_tf_data_fail_open"}
        return False, {"symbol": symbol, "side": side, "checks": checks, "reason": "no_tf_data"}
    return True, {"symbol": symbol, "side": side, "checks": checks, "reason": "ma5_third_bar_slope_ok"}


def _patched_build_scored_candidates(*args, **kwargs):
    candidates = _ORIG_BUILD(*args, **kwargs)  # type: ignore[misc]
    if not _env_bool("ENTRY_MA5_THIRD_BAR_SLOPE_GUARD_ENABLED", True):
        return candidates
    try:
        kept = []
        skipped = []
        for item in list(candidates or []):
            if not isinstance(item, dict):
                kept.append(item)
                continue
            ok, diag = _passes_ma5_guard(item)
            if ok:
                kept.append(item)
            else:
                skipped.append(diag)
        if skipped:
            logger.warning("[ENTRY MA5 THIRD BAR GUARD] filtered before=%s after=%s skipped=%s", len(list(candidates or [])), len(kept), skipped[:30])
        return kept
    except Exception:
        logger.exception("[ENTRY MA5 THIRD BAR GUARD] failed; fail-open")
        return candidates


def install() -> bool:
    global _INSTALLED, _ORIG_BUILD
    try:
        os.environ.setdefault("ENTRY_MA5_THIRD_BAR_STRONG_SCORE_FAILOPEN", "1")
        os.environ.setdefault("ENTRY_MA5_THIRD_BAR_STRONG_FAILOPEN_ALLOW_UNKNOWN_SOURCE", "1")
        os.environ.setdefault("ENTRY_MA5_THIRD_BAR_STRONG_FAILOPEN_MIN_SCORE", os.environ.get("ENTRY_MA5_THIRD_BAR_RANKING_FAILOPEN_MIN_SCORE", "75"))
        import trading.handlers.entry_controller as ec
        cur = getattr(ec, "_build_scored_candidates", None)
        if getattr(cur, "_entry_ma5_third_bar_guard_v3", False):
            _INSTALLED = True
            return True
        original = getattr(cur, "_original", None) if callable(cur) else None
        if callable(original):
            _ORIG_BUILD = original
        elif callable(cur):
            _ORIG_BUILD = cur
        else:
            logger.warning("[ENTRY MA5 THIRD BAR GUARD] target missing")
            return False
        _patched_build_scored_candidates._entry_ma5_third_bar_guard_v3 = True  # type: ignore[attr-defined]
        _patched_build_scored_candidates._entry_ma5_third_bar_guard_v2 = True  # type: ignore[attr-defined]
        _patched_build_scored_candidates._entry_ma5_third_bar_guard_v1 = True  # type: ignore[attr-defined]
        _patched_build_scored_candidates._original = _ORIG_BUILD  # type: ignore[attr-defined]
        ec._build_scored_candidates = _patched_build_scored_candidates
        _INSTALLED = True
        logger.warning(
            "[ENTRY MA5 THIRD BAR GUARD] installed v3 enabled=%s tfs=%s min_bars=%s fail_open=%s strong_failopen=%s min_score=%.2f allow_unknown_source=%s max_bad_slope_abs=%.3f",
            _env_bool("ENTRY_MA5_THIRD_BAR_SLOPE_GUARD_ENABLED", True),
            _tfs(),
            _env_int("ENTRY_MA5_THIRD_BAR_MIN_BARS", 3),
            _env_bool("ENTRY_MA5_THIRD_BAR_FAIL_OPEN", True),
            _env_bool("ENTRY_MA5_THIRD_BAR_STRONG_SCORE_FAILOPEN", True),
            _env_float("ENTRY_MA5_THIRD_BAR_STRONG_FAILOPEN_MIN_SCORE", 75.0),
            _env_bool("ENTRY_MA5_THIRD_BAR_STRONG_FAILOPEN_ALLOW_UNKNOWN_SOURCE", True),
            _env_float("ENTRY_MA5_THIRD_BAR_RANKING_MAX_BAD_SLOPE_ABS", 999999.0),
        )
        return True
    except Exception:
        logger.exception("[ENTRY MA5 THIRD BAR GUARD] install failed")
        return False

try:
    install()
except Exception:
    logger.exception("[ENTRY MA5 THIRD BAR GUARD] auto install failed")

__all__ = ["install"]
