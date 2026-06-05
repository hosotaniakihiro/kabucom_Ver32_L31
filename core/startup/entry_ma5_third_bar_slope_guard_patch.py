# ============================================================
# File   : core/startup/entry_ma5_third_bar_slope_guard_patch.py
# Version: V1-MA5-THIRD-BAR-SLOPE-CONFIRM
# ------------------------------------------------------------
# BUY:
#   3分足・5分足で価格がMA5を超えて1本目/2本目では入らず、
#   3本目でMA5傾きがプラスの時だけ許可する。
# SELL:
#   3分足・5分足で価格がMA5を下抜けて1本目/2本目では入らず、
#   3本目でMA5傾きがマイナスの時だけ許可する。
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


def _symbol_from_item(item: dict[str, Any]) -> str:
    try:
        for src in (item, item.get("entry_row"), item.get("entry"), item.get("row")):
            if isinstance(src, dict):
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
        for src in (item, item.get("entry_row"), item.get("entry"), item.get("row")):
            if isinstance(src, dict):
                s = str(src.get("side") or src.get("entry_decision") or "").strip().upper()
                if s in {"BUY", "SELL"}:
                    return s
    except Exception:
        pass
    return ""


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
            return False, {"symbol": symbol, "side": side, "checks": checks, "reason": "ma5_third_bar_slope_ng"}
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
        import trading.handlers.entry_controller as ec
        cur = getattr(ec, "_build_scored_candidates", None)
        if getattr(cur, "_entry_ma5_third_bar_guard_v1", False):
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
        _patched_build_scored_candidates._entry_ma5_third_bar_guard_v1 = True  # type: ignore[attr-defined]
        _patched_build_scored_candidates._original = _ORIG_BUILD  # type: ignore[attr-defined]
        ec._build_scored_candidates = _patched_build_scored_candidates
        _INSTALLED = True
        logger.warning("[ENTRY MA5 THIRD BAR GUARD] installed v1 enabled=%s tfs=%s min_bars=%s fail_open=%s", _env_bool("ENTRY_MA5_THIRD_BAR_SLOPE_GUARD_ENABLED", True), _tfs(), _env_int("ENTRY_MA5_THIRD_BAR_MIN_BARS", 3), _env_bool("ENTRY_MA5_THIRD_BAR_FAIL_OPEN", True))
        return True
    except Exception:
        logger.exception("[ENTRY MA5 THIRD BAR GUARD] install failed")
        return False

try:
    install()
except Exception:
    logger.exception("[ENTRY MA5 THIRD BAR GUARD] auto install failed")

__all__ = ["install"]
