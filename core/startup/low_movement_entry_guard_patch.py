# ============================================================
# File   : core/startup/low_movement_entry_guard_patch.py
# Version: Ver02-LOW-MOVEMENT-AND-RANKING-DIRECTION-GUARD
# ------------------------------------------------------------
# あまり動かない銘柄へのエントリーを発注直前で止める。
# さらに、ランキング方向に逆らうエントリーも禁止する。
#
# 仕様:
#   - 1,500〜2,999円: 高安幅/価格 >= 1.50% を要求
#   - 3,000〜7,000円: 高安幅/価格 >= 0.80% を要求
#   - 下落率ランキング/下落優勢銘柄への BUY を禁止
#   - 上昇率ランキング/上昇優勢銘柄への SELL を禁止
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)
_INSTALLED = False
_ORIG_ATR_FILTER = None
_ORIG_RANGE_FILTER = None


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return default
        return float(v)
    except Exception:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _row_to_dict(row: Any) -> dict:
    try:
        if row is None:
            return {}
        if isinstance(row, dict):
            return dict(row)
        if hasattr(row, "to_dict"):
            v = row.to_dict()
            if isinstance(v, dict):
                return dict(v)
        return {}
    except Exception:
        return {}


def _first(row: dict, keys: tuple[str, ...], default=None):
    for k in keys:
        try:
            v = row.get(k)
            if v is not None and str(v).strip() != "":
                return v
        except Exception:
            pass
    return default


def _norm_symbol(v: Any) -> str:
    try:
        s = str(v or "").strip()
        if s.endswith(".0") and s[:-2].isdigit():
            return s[:-2]
        return s
    except Exception:
        return ""


def _install_ranking_direction_guard() -> bool:
    try:
        from core.startup import ranking_direction_entry_guard_patch as p
        ok = p.install()
        logger.warning("[LOW MOVE GUARD] ranking_direction_entry_guard_patch installed=%s", ok)
        return bool(ok)
    except Exception:
        logger.exception("[LOW MOVE GUARD] ranking_direction_entry_guard_patch install failed")
        return False


def _low_movement_guard(entry_row: Any) -> bool:
    row = _row_to_dict(entry_row)
    symbol = _norm_symbol(_first(row, ("symbol", "code", "stock_code"), ""))
    close = _safe_float(_first(row, ("close_price", "close", "price", "current_price"), 0.0), 0.0)
    high = _safe_float(_first(row, ("high_price", "high"), 0.0), 0.0)
    low = _safe_float(_first(row, ("low_price", "low"), 0.0), 0.0)

    if close <= 0:
        logger.warning("[LOW MOVE GUARD] NG symbol=%s reason=no_close close=%s", symbol, close)
        return False

    if close < _env_float("LOW_MOVE_MIN_ENTRY_PRICE", 1500.0) or close > _env_float("LOW_MOVE_MAX_ENTRY_PRICE", 7000.0):
        logger.warning("[LOW MOVE GUARD] NG symbol=%s reason=price_out_of_range close=%.1f", symbol, close)
        return False

    if high <= 0 or low <= 0 or high < low:
        logger.warning("[LOW MOVE GUARD] NG symbol=%s reason=no_high_low close=%.1f high=%.1f low=%.1f", symbol, close, high, low)
        return False

    range_pct = (high - low) / close if close > 0 else 0.0
    split = _env_float("LOW_MOVE_TIER_SPLIT_PRICE", 3000.0)
    min_range_pct = _env_float("LOW_MOVE_MIN_RANGE_PCT_LOW_PRICE", 0.015) if close < split else _env_float("LOW_MOVE_MIN_RANGE_PCT_HIGH_PRICE", 0.008)

    if range_pct < min_range_pct:
        logger.warning("[LOW MOVE GUARD] NG symbol=%s reason=range_too_small close=%.1f high=%.1f low=%.1f range_pct=%.4f min=%.4f", symbol, close, high, low, range_pct, min_range_pct)
        return False

    slope_values = []
    for k in ("slope_atr_scaled", "slope", "score_slope", "disp_slope"):
        if k in row:
            slope_values.append(_safe_float(row.get(k), 0.0))

    if slope_values:
        abs_slope = max(abs(x) for x in slope_values)
        min_abs_slope = _env_float("LOW_MOVE_MIN_ABS_SLOPE_LOW_PRICE", 0.0008) if close < split else _env_float("LOW_MOVE_MIN_ABS_SLOPE_HIGH_PRICE", 0.0004)
        if abs_slope < min_abs_slope:
            logger.warning("[LOW MOVE GUARD] NG symbol=%s reason=slope_too_small close=%.1f abs_slope=%.6f min=%.6f range_pct=%.4f", symbol, close, abs_slope, min_abs_slope, range_pct)
            return False

    macd = _safe_float(row.get("macd"), 0.0)
    signal = _safe_float(row.get("signal"), 0.0)
    max_abs_slope = max([abs(x) for x in slope_values], default=0.0)
    if abs(macd) < 0.0001 and abs(signal) < 0.0001 and max_abs_slope < 0.0001:
        logger.warning("[LOW MOVE GUARD] NG symbol=%s reason=no_momentum macd=%.6f signal=%.6f slope=%.6f range_pct=%.4f", symbol, macd, signal, max_abs_slope, range_pct)
        return False

    logger.info("[LOW MOVE GUARD] OK symbol=%s close=%.1f range_pct=%.4f min_range=%.4f macd=%.4f signal=%.4f max_abs_slope=%.6f", symbol, close, range_pct, min_range_pct, macd, signal, max_abs_slope)
    return True


def _patched_range_5m_filter(entry_row: Any = None, *args, **kwargs):
    try:
        allow = True
        if callable(_ORIG_RANGE_FILTER):
            allow = _ORIG_RANGE_FILTER(entry_row, *args, **kwargs)
        if isinstance(allow, tuple):
            return allow
        if not bool(allow):
            return False
        if entry_row is not None:
            return _low_movement_guard(entry_row)
        return allow
    except Exception:
        logger.exception("[LOW MOVE GUARD] patched range filter failed")
        return False


def _patched_atr_1m_filter(entry_row: Any = None, *args, **kwargs):
    try:
        allow = True
        if callable(_ORIG_ATR_FILTER):
            allow = _ORIG_ATR_FILTER(entry_row, *args, **kwargs)
        if isinstance(allow, tuple):
            return allow
        if not bool(allow):
            return False
        if entry_row is not None:
            return _low_movement_guard(entry_row)
        return allow
    except Exception:
        logger.exception("[LOW MOVE GUARD] patched atr filter failed")
        return False


def install() -> bool:
    global _INSTALLED, _ORIG_ATR_FILTER, _ORIG_RANGE_FILTER
    ok_direction = _install_ranking_direction_guard()
    if _INSTALLED:
        return bool(ok_direction or True)
    try:
        import trading.handlers.entry_controller as ec
        old_atr = getattr(ec, "atr_1m_filter", None)
        old_range = getattr(ec, "range_5m_filter", None)
        if callable(old_range) and getattr(old_range, "_low_move_guard_v1", False):
            _INSTALLED = True
            return True
        _ORIG_ATR_FILTER = old_atr
        _ORIG_RANGE_FILTER = old_range
        _patched_atr_1m_filter._low_move_guard_v1 = True  # type: ignore[attr-defined]
        _patched_range_5m_filter._low_move_guard_v1 = True  # type: ignore[attr-defined]
        ec.atr_1m_filter = _patched_atr_1m_filter
        ec.range_5m_filter = _patched_range_5m_filter
        _INSTALLED = True
        logger.warning("[LOW MOVE GUARD] installed low_range=%.4f high_range=%.4f low_slope=%.6f high_slope=%.6f ranking_direction=%s", _env_float("LOW_MOVE_MIN_RANGE_PCT_LOW_PRICE", 0.015), _env_float("LOW_MOVE_MIN_RANGE_PCT_HIGH_PRICE", 0.008), _env_float("LOW_MOVE_MIN_ABS_SLOPE_LOW_PRICE", 0.0008), _env_float("LOW_MOVE_MIN_ABS_SLOPE_HIGH_PRICE", 0.0004), ok_direction)
        return True
    except Exception:
        logger.exception("[LOW MOVE GUARD] install failed")
        return bool(ok_direction)


try:
    install()
except Exception:
    logger.exception("[LOW MOVE GUARD] auto install failed")

__all__ = ["install"]
