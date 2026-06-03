# ============================================================
# File   : core/startup/entry_immediate_movement_guard_patch.py
# Version: V1.0-ENTRY-IMMEDIATE-MOVEMENT-GUARD
# ------------------------------------------------------------
# 目的:
#   エントリー後に株価が動かない銘柄を減らす。
#
#   AI_OK / liquidity OK でも、直近足の値幅・実体・slope・MACD方向が弱い場合は、
#   「入ってもすぐ動かない」可能性が高いため、発注候補から除外する。
#
# 方針:
#   - entry_controller._build_scored_candidates の返却候補を最終フィルタする。
#   - 1銘柄ごとに複数候補がある場合も、弱い候補だけ落とす。
#   - 強いスコア候補を完全に塞がないよう、major_mtf_score が高い場合は一部緩和。
# ============================================================
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)
_INSTALLED = False
_ORIG_BUILD = None

_TRUE = {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
_FALSE = {"0", "false", "no", "n", "off", "ng", "disable", "disabled"}


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        s = str(v).strip().lower()
        if s in _TRUE:
            return True
        if s in _FALSE:
            return False
    except Exception:
        pass
    return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return float(default)
        s = str(v).strip()
        if s == "" or s.lower() in {"none", "nan", "nat", "-"}:
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _safe_str(v: Any, default: str = "") -> str:
    try:
        if v is None:
            return default
        return str(v).strip()
    except Exception:
        return default


def _first(row: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for k in keys:
        if k in row and row.get(k) is not None:
            return row.get(k)
    return default


def _row_from_item(item: dict[str, Any]) -> dict[str, Any]:
    try:
        row = item.get("entry_row")
        if isinstance(row, dict):
            return row
        ent = item.get("entry")
        if isinstance(ent, dict):
            return ent
    except Exception:
        pass
    return {}


def _calc_diag(item: dict[str, Any]) -> dict[str, Any]:
    row = _row_from_item(item)
    side = _safe_str(item.get("side") or row.get("side") or row.get("entry_decision")).upper()
    symbol = _safe_str(item.get("symbol") or row.get("symbol"))
    source = _safe_str(row.get("source") or item.get("entry_type") or item.get("source")).upper()

    open_p = _safe_float(_first(row, "open", "open_price"), 0.0)
    high_p = _safe_float(_first(row, "high", "high_price"), 0.0)
    low_p = _safe_float(_first(row, "low", "low_price"), 0.0)
    close_p = _safe_float(_first(row, "close", "close_price", "price", "current_price"), 0.0)

    base = close_p if close_p > 0 else max(open_p, high_p, low_p, 1.0)
    range_pct = ((high_p - low_p) / base * 100.0) if high_p > 0 and low_p > 0 and high_p >= low_p and base > 0 else 0.0
    body_pct = ((close_p - open_p) / open_p * 100.0) if open_p > 0 and close_p > 0 else 0.0

    slope = _safe_float(_first(row, "slope", "disp_slope"), 0.0)
    slope_atr = _safe_float(_first(row, "slope_atr_scaled", "disp_slope_atr_scaled"), 0.0)
    score_slope = _safe_float(_first(row, "score_slope"), 0.0)
    macd = _safe_float(_first(row, "macd"), 0.0)
    signal = _safe_float(_first(row, "signal"), 0.0)
    rsi = _safe_float(_first(row, "rsi"), 50.0)
    volume = _safe_float(_first(row, "volume", "latest_volume", "trading_volume"), 0.0)
    tick_count = _safe_float(_first(row, "tick_count", "ticks"), 0.0)
    mtf = _safe_float(_first(row, "mtf", "score_mtf", "mtf_score"), 0.0)
    score = abs(_safe_float(_first(row, "score", "score_total", "final_score", "display_score"), 0.0))
    priority = _safe_float(item.get("priority_score"), 0.0)

    if side == "SELL":
        body_dir = -body_pct
        slope_dir = -slope
        slope_atr_dir = -slope_atr
        score_slope_dir = -score_slope
        macd_dir = signal - macd
        rsi_dir_ok = rsi <= _env_float("ENTRY_MOVE_SELL_MAX_RSI", 55.0)
    else:
        body_dir = body_pct
        slope_dir = slope
        slope_atr_dir = slope_atr
        score_slope_dir = score_slope
        macd_dir = macd - signal
        rsi_dir_ok = rsi >= _env_float("ENTRY_MOVE_BUY_MIN_RSI", 45.0)

    return {
        "symbol": symbol,
        "side": side,
        "source": source,
        "open": open_p,
        "high": high_p,
        "low": low_p,
        "close": close_p,
        "range_pct": range_pct,
        "body_pct": body_pct,
        "body_dir": body_dir,
        "slope": slope,
        "slope_dir": slope_dir,
        "slope_atr_scaled": slope_atr,
        "slope_atr_dir": slope_atr_dir,
        "score_slope": score_slope,
        "score_slope_dir": score_slope_dir,
        "macd": macd,
        "signal": signal,
        "macd_dir": macd_dir,
        "rsi": rsi,
        "rsi_dir_ok": rsi_dir_ok,
        "volume": volume,
        "tick_count": tick_count,
        "mtf": mtf,
        "score": score,
        "priority": priority,
    }


def _movement_ok(item: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    d = _calc_diag(item)
    side = d.get("side")
    source = _safe_str(d.get("source")).upper()

    if side not in {"BUY", "SELL"}:
        return False, {**d, "ng": "side_invalid"}

    # RANKING/TONOSAMAはすでに勢い系フィルタが厚いので少し緩める。
    is_fast_source = source in {"RANKING", "RANKING_5S", "TONOSAMA", "EARLY_SCALP"}

    min_range = _env_float("ENTRY_MOVE_MIN_RANGE_PCT_FAST", 0.20) if is_fast_source else _env_float("ENTRY_MOVE_MIN_RANGE_PCT_SUMMARY", 0.28)
    min_body = _env_float("ENTRY_MOVE_MIN_BODY_DIR_PCT_FAST", 0.03) if is_fast_source else _env_float("ENTRY_MOVE_MIN_BODY_DIR_PCT_SUMMARY", 0.05)
    min_slope_atr = _env_float("ENTRY_MOVE_MIN_SLOPE_ATR_FAST", 0.0004) if is_fast_source else _env_float("ENTRY_MOVE_MIN_SLOPE_ATR_SUMMARY", 0.0006)
    min_score_slope = _env_float("ENTRY_MOVE_MIN_SCORE_SLOPE", 0.04)
    min_macd_dir = _env_float("ENTRY_MOVE_MIN_MACD_DIR", -0.02)
    strong_mtf = _env_float("ENTRY_MOVE_STRONG_MTF_RELAX", 6.0)
    strong_score = _env_float("ENTRY_MOVE_STRONG_SCORE_RELAX", 5.5)

    range_ok = float(d["range_pct"]) >= min_range
    body_ok = float(d["body_dir"]) >= min_body
    slope_ok = float(d["slope_atr_dir"]) >= min_slope_atr or float(d["score_slope_dir"]) >= min_score_slope
    macd_ok = float(d["macd_dir"]) >= min_macd_dir
    rsi_ok = bool(d["rsi_dir_ok"])

    # かなり強いMTF/scoreなら、bodyが小さくてもrange+slopeで許可。
    strong_context = float(d["mtf"]) >= strong_mtf or float(d["score"]) >= strong_score or float(d["priority"]) >= strong_score

    if range_ok and rsi_ok and macd_ok and (body_ok or slope_ok):
        return True, {**d, "ok": "range_body_or_slope"}
    if strong_context and range_ok and slope_ok and macd_ok:
        return True, {**d, "ok": "strong_context_range_slope"}

    return False, {
        **d,
        "ng": "immediate_movement_weak",
        "range_ok": range_ok,
        "body_ok": body_ok,
        "slope_ok": slope_ok,
        "macd_ok": macd_ok,
        "rsi_ok": rsi_ok,
        "strong_context": strong_context,
        "min_range": min_range,
        "min_body": min_body,
        "min_slope_atr": min_slope_atr,
        "min_score_slope": min_score_slope,
        "min_macd_dir": min_macd_dir,
    }


def _patched_build_scored_candidates(*args, **kwargs):
    candidates = _ORIG_BUILD(*args, **kwargs)  # type: ignore[misc]
    if not _env_bool("ENTRY_IMMEDIATE_MOVEMENT_GUARD_ENABLED", True):
        return candidates
    try:
        src = _safe_str(kwargs.get("pipeline_source") or "").upper()
        if src and src not in {x.strip().upper() for x in os.getenv("ENTRY_MOVE_GUARD_SOURCES", "SUMMARY,RANKING,TONOSAMA,EARLY_SCALP").split(",") if x.strip()}:
            return candidates

        kept: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for item in list(candidates or []):
            if not isinstance(item, dict):
                kept.append(item)
                continue
            ok, diag = _movement_ok(item)
            if ok:
                kept.append(item)
            else:
                skipped.append({
                    "symbol": diag.get("symbol"),
                    "side": diag.get("side"),
                    "source": diag.get("source"),
                    "ng": diag.get("ng"),
                    "range_pct": round(float(diag.get("range_pct") or 0.0), 4),
                    "body_dir": round(float(diag.get("body_dir") or 0.0), 4),
                    "slope_atr_dir": round(float(diag.get("slope_atr_dir") or 0.0), 6),
                    "score_slope_dir": round(float(diag.get("score_slope_dir") or 0.0), 4),
                    "macd_dir": round(float(diag.get("macd_dir") or 0.0), 4),
                    "rsi": round(float(diag.get("rsi") or 0.0), 2),
                    "mtf": round(float(diag.get("mtf") or 0.0), 3),
                    "score": round(float(diag.get("score") or 0.0), 3),
                })
        if skipped:
            logger.warning(
                "[ENTRY IMMEDIATE MOVE GUARD] filtered before=%s after=%s skipped=%s",
                len(list(candidates or [])),
                len(kept),
                skipped[:50],
            )
        return kept
    except Exception:
        logger.exception("[ENTRY IMMEDIATE MOVE GUARD] failed; fail-open")
        return candidates


def install() -> bool:
    global _INSTALLED, _ORIG_BUILD
    if _INSTALLED:
        return True
    try:
        import trading.handlers.entry_controller as ec
        cur = getattr(ec, "_build_scored_candidates", None)
        if not callable(cur):
            logger.warning("[ENTRY IMMEDIATE MOVE GUARD] target missing")
            return False
        if getattr(cur, "_entry_immediate_movement_guard_v1", False):
            _INSTALLED = True
            return True
        _ORIG_BUILD = getattr(cur, "_original", None) if hasattr(cur, "_original") else cur
        if not callable(_ORIG_BUILD):
            _ORIG_BUILD = cur
        _patched_build_scored_candidates._entry_immediate_movement_guard_v1 = True  # type: ignore[attr-defined]
        _patched_build_scored_candidates._original = _ORIG_BUILD  # type: ignore[attr-defined]
        ec._build_scored_candidates = _patched_build_scored_candidates
        _INSTALLED = True
        logger.warning(
            "[ENTRY IMMEDIATE MOVE GUARD] installed v1 enabled=%s sources=%s summary_min_range=%.3f summary_min_body=%.3f summary_min_slope_atr=%.6f",
            _env_bool("ENTRY_IMMEDIATE_MOVEMENT_GUARD_ENABLED", True),
            os.getenv("ENTRY_MOVE_GUARD_SOURCES", "SUMMARY,RANKING,TONOSAMA,EARLY_SCALP"),
            _env_float("ENTRY_MOVE_MIN_RANGE_PCT_SUMMARY", 0.28),
            _env_float("ENTRY_MOVE_MIN_BODY_DIR_PCT_SUMMARY", 0.05),
            _env_float("ENTRY_MOVE_MIN_SLOPE_ATR_SUMMARY", 0.0006),
        )
        return True
    except Exception:
        logger.exception("[ENTRY IMMEDIATE MOVE GUARD] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[ENTRY IMMEDIATE MOVE GUARD] auto install failed")

__all__ = ["install"]
