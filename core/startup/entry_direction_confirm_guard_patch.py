# ============================================================
# File   : core/startup/entry_direction_confirm_guard_patch.py
# Version: Ver01-ENTRY-DIRECTION-CONFIRM-GUARD
# ------------------------------------------------------------
# 「売ったら上がる / 買ったら下がる」を減らすため、
# エントリー直前で方向確認を行う runtime patch。
#
# 方針:
#   BUY  : 直近方向が上向きでなければ止める
#   SELL : 直近方向が下向きでなければ止める
#
# 判定材料:
#   - side
#   - slope / slope_atr_scaled / score_slope
#   - macd - signal
#   - close と open の方向
#   - close のバー内位置 high/low
#   - score_buy / score_sell の優劣
#   - flag_score_total_delta / pattern_score_delta
#
# 環境変数:
#   ENTRY_DIRECTION_CONFIRM_ENABLED=1
#   ENTRY_DIRECTION_CONFIRM_MIN_STRENGTH=1.5
#   ENTRY_DIRECTION_CONFIRM_STRICT=1
#
# ログ:
#   [ENTRY DIRECTION CONFIRM] OK/NG ...
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_INSTALLED = False
_ORIG_ATR_FILTER = None
_ORIG_RANGE_FILTER = None


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None:
            return bool(default)
        s = str(v).strip().lower()
        if s in {"1", "true", "yes", "y", "on"}:
            return True
        if s in {"0", "false", "no", "n", "off", ""}:
            return False
        return bool(default)
    except Exception:
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
            d = row.to_dict()
            if isinstance(d, dict):
                return dict(d)
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


def _norm_side(v: Any) -> str:
    s = str(v or "").strip().upper()
    if s in {"1", "SELL", "SHORT", "売", "売り"}:
        return "SELL"
    if s in {"2", "BUY", "LONG", "買", "買い"}:
        return "BUY"
    return s


def _norm_symbol(v: Any) -> str:
    s = str(v or "").strip()
    if s.endswith(".0") and s[:-2].isdigit():
        return s[:-2]
    return s


def _calc_direction_strength(row: dict) -> tuple[float, list[str]]:
    reasons: list[str] = []
    strength = 0.0

    close = _safe_float(_first(row, ("close", "close_price", "price", "current_price"), 0.0), 0.0)
    open_ = _safe_float(_first(row, ("open", "open_price"), 0.0), 0.0)
    high = _safe_float(_first(row, ("high", "high_price"), 0.0), 0.0)
    low = _safe_float(_first(row, ("low", "low_price"), 0.0), 0.0)

    slope = _safe_float(_first(row, ("slope_atr_scaled", "score_slope", "disp_slope", "slope"), 0.0), 0.0)
    mtf = _safe_float(_first(row, ("score_mtf", "mtf_score", "disp_mtf", "mtf"), 0.0), 0.0)
    macd = _safe_float(row.get("macd"), 0.0)
    signal = _safe_float(row.get("signal"), 0.0)
    hist = _safe_float(row.get("hist"), macd - signal)

    score_buy = _safe_float(_first(row, ("score_buy", "buy_score", "disp_buy_score", "buy"), 0.0), 0.0)
    score_sell = _safe_float(_first(row, ("score_sell", "sell_score", "disp_sell_score", "sell"), 0.0), 0.0)
    flag_delta = _safe_float(row.get("flag_score_total_delta"), 0.0)
    pattern_delta = _safe_float(row.get("pattern_score_delta"), 0.0)

    if slope > 0:
        strength += 1.0
        reasons.append("slope_up")
    elif slope < 0:
        strength -= 1.0
        reasons.append("slope_down")

    if mtf > 0:
        strength += 0.7
        reasons.append("mtf_up")
    elif mtf < 0:
        strength -= 0.7
        reasons.append("mtf_down")

    macd_diff = hist if hist != 0 else macd - signal
    if macd_diff > 0:
        strength += 0.8
        reasons.append("macd_up")
    elif macd_diff < 0:
        strength -= 0.8
        reasons.append("macd_down")

    if close > 0 and open_ > 0:
        oc_pct = (close - open_) / close
        if oc_pct > 0.001:
            strength += 0.8
            reasons.append("bar_green")
        elif oc_pct < -0.001:
            strength -= 0.8
            reasons.append("bar_red")

    if high > low and close > 0:
        pos = (close - low) / max(high - low, 1e-9)
        if pos >= 0.70:
            strength += 0.6
            reasons.append("close_near_high")
        elif pos <= 0.30:
            strength -= 0.6
            reasons.append("close_near_low")

    if score_buy > score_sell:
        strength += 0.8
        reasons.append("buy_score_dominant")
    elif score_sell > score_buy:
        strength -= 0.8
        reasons.append("sell_score_dominant")

    if flag_delta > 0:
        strength += min(1.0, abs(flag_delta) / 10.0)
        reasons.append("flag_delta_up")
    elif flag_delta < 0:
        strength -= min(1.0, abs(flag_delta) / 10.0)
        reasons.append("flag_delta_down")

    if pattern_delta > 0:
        strength += min(1.0, abs(pattern_delta) / 10.0)
        reasons.append("pattern_up")
    elif pattern_delta < 0:
        strength -= min(1.0, abs(pattern_delta) / 10.0)
        reasons.append("pattern_down")

    return float(strength), reasons


def _direction_confirm(entry_row: Any) -> bool:
    if not _env_bool("ENTRY_DIRECTION_CONFIRM_ENABLED", True):
        return True

    row = _row_to_dict(entry_row)
    side = _norm_side(_first(row, ("side", "売買", "order_side"), ""))
    symbol = _norm_symbol(_first(row, ("symbol", "code", "stock_code"), ""))
    min_strength = _env_float("ENTRY_DIRECTION_CONFIRM_MIN_STRENGTH", 1.5)
    strict = _env_bool("ENTRY_DIRECTION_CONFIRM_STRICT", True)

    if side not in {"BUY", "SELL"}:
        logger.warning("[ENTRY DIRECTION CONFIRM] SKIP symbol=%s reason=unknown_side side=%s", symbol, side)
        return True

    strength, reasons = _calc_direction_strength(row)

    if side == "BUY":
        ok = strength >= min_strength
    else:
        ok = strength <= -min_strength

    if not ok and not strict:
        weak = abs(strength) >= min_strength * 0.5
        if weak:
            logger.warning("[ENTRY DIRECTION CONFIRM] WEAK_ALLOW symbol=%s side=%s strength=%.3f min=%.3f reasons=%s", symbol, side, strength, min_strength, reasons)
            return True

    if ok:
        logger.info("[ENTRY DIRECTION CONFIRM] OK symbol=%s side=%s strength=%.3f min=%.3f reasons=%s", symbol, side, strength, min_strength, reasons)
        return True

    logger.warning("[ENTRY DIRECTION CONFIRM] NG symbol=%s side=%s strength=%.3f min=%.3f reasons=%s", symbol, side, strength, min_strength, reasons)
    return False


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
            return _direction_confirm(entry_row)
        return allow
    except Exception:
        logger.exception("[ENTRY DIRECTION CONFIRM] patched range filter failed")
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
            return _direction_confirm(entry_row)
        return allow
    except Exception:
        logger.exception("[ENTRY DIRECTION CONFIRM] patched atr filter failed")
        return False


def install() -> bool:
    global _INSTALLED, _ORIG_ATR_FILTER, _ORIG_RANGE_FILTER
    if _INSTALLED:
        return True
    try:
        import trading.handlers.entry_controller as ec
        old_atr = getattr(ec, "atr_1m_filter", None)
        old_range = getattr(ec, "range_5m_filter", None)
        if callable(old_range) and getattr(old_range, "_entry_direction_confirm_guard", False):
            _INSTALLED = True
            return True
        _ORIG_ATR_FILTER = old_atr
        _ORIG_RANGE_FILTER = old_range
        _patched_atr_1m_filter._entry_direction_confirm_guard = True  # type: ignore[attr-defined]
        _patched_range_5m_filter._entry_direction_confirm_guard = True  # type: ignore[attr-defined]
        ec.atr_1m_filter = _patched_atr_1m_filter
        ec.range_5m_filter = _patched_range_5m_filter
        _INSTALLED = True
        logger.warning(
            "[ENTRY DIRECTION CONFIRM] installed enabled=%s min_strength=%.3f strict=%s",
            _env_bool("ENTRY_DIRECTION_CONFIRM_ENABLED", True),
            _env_float("ENTRY_DIRECTION_CONFIRM_MIN_STRENGTH", 1.5),
            _env_bool("ENTRY_DIRECTION_CONFIRM_STRICT", True),
        )
        return True
    except Exception:
        logger.exception("[ENTRY DIRECTION CONFIRM] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[ENTRY DIRECTION CONFIRM] auto install failed")

__all__ = ["install"]
