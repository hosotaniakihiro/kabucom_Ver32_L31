# ============================================================
# File   : core/startup/entry_direction_confirm_guard_patch.py
# Version: Ver06-CLIMAX-REVERSAL-EXCEPTION
# ------------------------------------------------------------
# エントリー直前の方向確認 runtime patch。
#
# 主なガード:
#   1. MA構造ガード
#      BUY : 株価がMA下、MA並びが下落形ならBUY禁止
#      SELL: 株価がMA上、MA並びが上昇形ならSELL禁止
#
#   2. MA傾きガード
#      daily_ma25_slope / daily_ma75_slope などを読む
#
#   3. MA乖離率ガード
#      BUY : MA25/MA75より大きく下に乖離している場合はBUY禁止
#      SELL: MA25/MA75より大きく上に乖離している場合はSELL禁止
#
#   4. 分足クライマックス反転例外
#      selling_climax_absorption / selling_climax_wick ならBUY例外許可
#      buying_climax_exhaustion / buying_climax_wick ならSELL例外許可
#
# 注意:
#   クライマックス反転は無条件許可ではない。
#   出来高・売買代金・MA乖離・トレンド失速などが複合成立した場合のみ、
#   MA構造/MA乖離/方向強度NGを例外的に通す。
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


def _has_any(row: dict, keys: tuple[str, ...]) -> bool:
    for k in keys:
        try:
            if k in row and row.get(k) is not None and str(row.get(k)).strip() != "":
                return True
        except Exception:
            pass
    return False


def _norm_side(v: Any) -> str:
    s = str(v or "").strip().upper()
    if s in {"1", "SELL", "SHORT", "売", "売り"}:
        return "SELL"
    if s in {"2", "BUY", "LONG", "買", "買い"}:
        return "BUY"
    return s


def _opposite_side(side: str) -> str:
    side = _norm_side(side)
    if side == "BUY":
        return "SELL"
    if side == "SELL":
        return "BUY"
    return side


def _norm_symbol(v: Any) -> str:
    s = str(v or "").strip()
    if s.endswith(".0") and s[:-2].isdigit():
        return s[:-2]
    return s


def _set_row_value(entry_row: Any, key: str, value: Any) -> None:
    try:
        if isinstance(entry_row, dict):
            entry_row[key] = value
            return
        if hasattr(entry_row, "__setitem__"):
            entry_row[key] = value
    except Exception:
        return


def _get_price_ma(row: dict) -> tuple[float, float, float, float, str]:
    close = _safe_float(_first(row, ("close", "close_price", "price", "current_price", "daily_close"), 0.0), 0.0)

    minute_ma_exists = any(k in row for k in ("ma5", "MA5", "ma_5", "ma25", "MA25", "ma_25", "ma75", "MA75", "ma_75"))
    daily_ma_exists = any(k in row for k in ("daily_ma5", "daily_ma25", "daily_ma75", "MA_5", "MA_25", "MA_75"))

    ma5_raw = _first(row, ("ma5", "MA5", "ma_5", "daily_ma5", "MA_5"), None)
    ma25_raw = _first(row, ("ma25", "MA25", "ma_25", "daily_ma25", "MA_25"), None)
    ma75_raw = _first(row, ("ma75", "MA75", "ma_75", "daily_ma75", "MA_75"), None)

    ma5 = _safe_float(ma5_raw, 0.0)
    ma25 = _safe_float(ma25_raw, 0.0)
    ma75 = _safe_float(ma75_raw, 0.0)

    if minute_ma_exists and daily_ma_exists:
        src = "minute_or_daily"
    elif minute_ma_exists:
        src = "minute"
    elif daily_ma_exists:
        src = "daily"
    else:
        src = "none"

    return close, ma5, ma25, ma75, src


def _ma_slope_keys(ma_key: str) -> tuple[str, ...]:
    n = ma_key.lower().replace("ma", "")
    return (
        f"{ma_key}_slope",
        f"{ma_key}_Slope",
        f"{ma_key.upper()}_Slope",
        f"{ma_key.upper()}_slope",
        f"ma_{n}_slope",
        f"MA_{n}_Slope",
        f"MA_{n}_slope",
        f"daily_{ma_key}_slope",
        f"daily_{ma_key}_Slope",
        f"daily_ma{n}_slope",
        f"daily_ma{n}_Slope",
        f"daily_MA_{n}_Slope",
    )


def _get_ma_slope(row: dict, ma_key: str) -> tuple[float, bool]:
    keys = _ma_slope_keys(ma_key)
    found = _has_any(row, keys)
    if not found:
        return 0.0, False
    return _safe_float(_first(row, keys, 0.0), 0.0), True


def _ma_dev_pct(close: float, ma: float) -> float:
    try:
        if close <= 0 or ma <= 0:
            return 0.0
        return ((close - ma) / ma) * 100.0
    except Exception:
        return 0.0


def _climax_exception(entry_row: Any, row: dict, side: str, symbol: str, blocked_by: str) -> bool:
    """
    通常ガードでNGになった場合でも、分足クライマックス反転が成立していれば例外許可する。
    """
    if not _env_bool("ENTRY_CLIMAX_REVERSAL_EXCEPTION_ENABLED", True):
        return False

    try:
        from trading.entry.climax_reversal_detector import detect_climax_reversal

        res = detect_climax_reversal(row, side=side)
        allow = bool(res.get("allow_exception"))
        ctype = str(res.get("climax_type") or "")
        score = _safe_float(res.get("climax_score"), 0.0)
        reason = str(res.get("reason") or "")

        if allow:
            _set_row_value(entry_row, "climax_reversal_exception", True)
            _set_row_value(entry_row, "climax_type", ctype)
            _set_row_value(entry_row, "climax_score", score)
            _set_row_value(entry_row, "climax_reason", reason)
            _set_row_value(entry_row, "climax_blocked_by", blocked_by)
            logger.warning(
                "[ENTRY CLIMAX EXCEPTION] ALLOW symbol=%s side=%s blocked_by=%s type=%s score=%.2f reason=%s",
                symbol, side, blocked_by, ctype, score, reason,
            )
            return True

        logger.info(
            "[ENTRY CLIMAX EXCEPTION] NO symbol=%s side=%s blocked_by=%s reason=%s score=%.2f type=%s",
            symbol, side, blocked_by, reason, score, ctype,
        )
        return False

    except Exception:
        logger.exception(
            "[ENTRY CLIMAX EXCEPTION] failed symbol=%s side=%s blocked_by=%s",
            symbol, side, blocked_by,
        )
        return False


def _ma_deviation_guard(row: dict, side: str, symbol: str) -> bool:
    if not _env_bool("ENTRY_MA_DEVIATION_GUARD_ENABLED", True):
        return True

    close, _ma5, ma25, ma75, ma_source = _get_price_ma(row)
    if close <= 0 or ma25 <= 0 or ma75 <= 0:
        logger.info(
            "[ENTRY MA DEVIATION GUARD] SKIP symbol=%s side=%s reason=ma_missing close=%.4f ma25=%.4f ma75=%.4f ma_source=%s",
            symbol, side, close, ma25, ma75, ma_source,
        )
        return True

    dev25 = _ma_dev_pct(close, ma25)
    dev75 = _ma_dev_pct(close, ma75)

    buy_ma25_limit = _env_float("ENTRY_BUY_MAX_NEGATIVE_MA25_DEV_PCT", -1.0)
    buy_ma75_limit = _env_float("ENTRY_BUY_MAX_NEGATIVE_MA75_DEV_PCT", -1.5)
    sell_ma25_limit = _env_float("ENTRY_SELL_MAX_POSITIVE_MA25_DEV_PCT", 1.0)
    sell_ma75_limit = _env_float("ENTRY_SELL_MAX_POSITIVE_MA75_DEV_PCT", 1.5)

    if side == "BUY" and (dev25 <= buy_ma25_limit or dev75 <= buy_ma75_limit):
        logger.warning(
            "[ENTRY MA DEVIATION GUARD] NG symbol=%s side=BUY reason=negative_ma_deviation close=%.4f ma25=%.4f ma75=%.4f dev25=%.3f%% limit25=%.3f%% dev75=%.3f%% limit75=%.3f%% ma_source=%s",
            symbol, close, ma25, ma75, dev25, buy_ma25_limit, dev75, buy_ma75_limit, ma_source,
        )
        return False

    if side == "SELL" and (dev25 >= sell_ma25_limit or dev75 >= sell_ma75_limit):
        logger.warning(
            "[ENTRY MA DEVIATION GUARD] NG symbol=%s side=SELL reason=positive_ma_deviation close=%.4f ma25=%.4f ma75=%.4f dev25=%.3f%% limit25=%.3f%% dev75=%.3f%% limit75=%.3f%% ma_source=%s",
            symbol, close, ma25, ma75, dev25, sell_ma25_limit, dev75, sell_ma75_limit, ma_source,
        )
        return False

    logger.info(
        "[ENTRY MA DEVIATION GUARD] OK symbol=%s side=%s close=%.4f ma25=%.4f ma75=%.4f dev25=%.3f%% dev75=%.3f%% ma_source=%s",
        symbol, side, close, ma25, ma75, dev25, dev75, ma_source,
    )
    return True


def _ma_structure_guard(row: dict, side: str, symbol: str) -> bool:
    if not _env_bool("ENTRY_MA_STRUCTURE_GUARD_ENABLED", True):
        return True

    close, ma5, ma25, ma75, ma_source = _get_price_ma(row)
    if close <= 0 or ma5 <= 0 or ma25 <= 0 or ma75 <= 0:
        logger.info(
            "[ENTRY MA STRUCTURE GUARD] SKIP symbol=%s side=%s reason=ma_missing close=%.4f ma5=%.4f ma25=%.4f ma75=%.4f ma_source=%s",
            symbol, side, close, ma5, ma25, ma75, ma_source,
        )
        return True

    ma25_slope, has_ma25_slope = _get_ma_slope(row, "ma25")
    ma75_slope, has_ma75_slope = _get_ma_slope(row, "ma75")
    has_any_ma_slope = has_ma25_slope or has_ma75_slope
    block_order_only = _env_bool("ENTRY_MA_ORDER_ONLY_BLOCK_WHEN_SLOPE_MISSING", True)

    bearish_price = close < ma5 and close < ma25 and close < ma75
    bearish_order = ma5 <= ma25 <= ma75
    bearish_ma_slope = (ma25_slope <= 0 if has_ma25_slope else False) or (ma75_slope <= 0 if has_ma75_slope else False)
    bearish_block = bearish_price and bearish_order and (bearish_ma_slope or (block_order_only and not has_any_ma_slope))

    bullish_price = close > ma5 and close > ma25 and close > ma75
    bullish_order = ma5 >= ma25 >= ma75
    bullish_ma_slope = (ma25_slope >= 0 if has_ma25_slope else False) or (ma75_slope >= 0 if has_ma75_slope else False)
    bullish_block = bullish_price and bullish_order and (bullish_ma_slope or (block_order_only and not has_any_ma_slope))

    if side == "BUY" and _env_bool("ENTRY_MA_REQUIRE_PRICE_ABOVE_ALL_FOR_BUY", True):
        if bearish_block:
            reason = "bearish_ma_structure_no_slope" if not has_any_ma_slope else "bearish_ma_structure"
            logger.warning(
                "[ENTRY MA STRUCTURE GUARD] NG symbol=%s side=BUY reason=%s close=%.4f ma5=%.4f ma25=%.4f ma75=%.4f ma_source=%s ma25_slope=%.6f ma75_slope=%.6f has_ma25_slope=%s has_ma75_slope=%s",
                symbol, reason, close, ma5, ma25, ma75, ma_source, ma25_slope, ma75_slope, has_ma25_slope, has_ma75_slope,
            )
            return False

    if side == "SELL" and _env_bool("ENTRY_MA_REQUIRE_PRICE_BELOW_ALL_FOR_SELL", True):
        if bullish_block:
            reason = "bullish_ma_structure_no_slope" if not has_any_ma_slope else "bullish_ma_structure"
            logger.warning(
                "[ENTRY MA STRUCTURE GUARD] NG symbol=%s side=SELL reason=%s close=%.4f ma5=%.4f ma25=%.4f ma75=%.4f ma_source=%s ma25_slope=%.6f ma75_slope=%.6f has_ma25_slope=%s has_ma75_slope=%s",
                symbol, reason, close, ma5, ma25, ma75, ma_source, ma25_slope, ma75_slope, has_ma25_slope, has_ma75_slope,
            )
            return False

    logger.info(
        "[ENTRY MA STRUCTURE GUARD] OK symbol=%s side=%s close=%.4f ma5=%.4f ma25=%.4f ma75=%.4f ma_source=%s ma25_slope=%.6f ma75_slope=%.6f has_ma25_slope=%s has_ma75_slope=%s order_only_block=%s",
        symbol, side, close, ma5, ma25, ma75, ma_source, ma25_slope, ma75_slope, has_ma25_slope, has_ma75_slope, block_order_only,
    )
    return True


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

    close2, ma5, ma25, ma75, _ma_source = _get_price_ma(row)
    if close2 > 0 and ma5 > 0 and ma25 > 0 and ma75 > 0:
        if close2 > ma5 > ma25 > ma75:
            strength += 1.2
            reasons.append("ma_bullish_order")
        elif close2 < ma5 < ma25 < ma75:
            strength -= 1.2
            reasons.append("ma_bearish_order")
        elif close2 > ma25 and close2 > ma75:
            strength += 0.5
            reasons.append("price_above_ma25_75")
        elif close2 < ma25 and close2 < ma75:
            strength -= 0.5
            reasons.append("price_below_ma25_75")

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


def _try_reverse_entry_side(entry_row: Any, row: dict, side: str, strength: float, reasons: list[str]) -> bool:
    if not _env_bool("ENTRY_CONTRARIAN_REVERSE_ENABLED", True):
        return False

    reverse_min = _env_float("ENTRY_CONTRARIAN_REVERSE_MIN_STRENGTH", 2.5)
    if abs(strength) < reverse_min:
        return False

    original_side = _norm_side(side)
    reverse_side = _opposite_side(original_side)
    if reverse_side not in {"BUY", "SELL"}:
        return False

    if original_side == "BUY" and strength > -reverse_min:
        return False
    if original_side == "SELL" and strength < reverse_min:
        return False

    symbol = _norm_symbol(_first(row, ("symbol", "code", "stock_code"), ""))

    _set_row_value(entry_row, "side", reverse_side)
    _set_row_value(entry_row, "entry_decision", reverse_side)
    _set_row_value(entry_row, "ai_side", reverse_side)
    _set_row_value(entry_row, "contrarian_reversed", True)
    _set_row_value(entry_row, "original_side", original_side)
    _set_row_value(entry_row, "reverse_reason", "direction_failed_strong_opposite")
    _set_row_value(entry_row, "reverse_strength", float(strength))

    if _env_bool("ENTRY_CONTRARIAN_REVERSE_HALF_SIZE", True):
        for key in ("lot_multiplier", "qty_multiplier"):
            old = _safe_float(row.get(key), 1.0)
            if old > 0:
                _set_row_value(entry_row, key, max(0.5, old * 0.5))
        _set_row_value(entry_row, "reverse_half_size", True)

    logger.warning(
        "[ENTRY CONTRARIAN REVERSE] symbol=%s %s->%s strength=%.3f min=%.3f reasons=%s",
        symbol,
        original_side,
        reverse_side,
        strength,
        reverse_min,
        reasons,
    )
    return True


def _direction_confirm(entry_row: Any) -> bool:
    if not _env_bool("ENTRY_DIRECTION_CONFIRM_ENABLED", True):
        return True

    row = _row_to_dict(entry_row)
    side = _norm_side(_first(row, ("side", "売買", "order_side", "entry_decision", "ai_side"), ""))
    symbol = _norm_symbol(_first(row, ("symbol", "code", "stock_code"), ""))
    min_strength = _env_float("ENTRY_DIRECTION_CONFIRM_MIN_STRENGTH", 1.5)
    strict = _env_bool("ENTRY_DIRECTION_CONFIRM_STRICT", True)

    if side not in {"BUY", "SELL"}:
        logger.warning("[ENTRY DIRECTION CONFIRM] SKIP symbol=%s reason=unknown_side side=%s", symbol, side)
        return True

    if not _ma_structure_guard(row, side, symbol):
        if not _climax_exception(entry_row, row, side, symbol, "ma_structure"):
            return False

    if not _ma_deviation_guard(row, side, symbol):
        if not _climax_exception(entry_row, row, side, symbol, "ma_deviation"):
            return False

    strength, reasons = _calc_direction_strength(row)

    ok = strength >= min_strength if side == "BUY" else strength <= -min_strength
    if ok:
        logger.info("[ENTRY DIRECTION CONFIRM] OK symbol=%s side=%s strength=%.3f min=%.3f reasons=%s", symbol, side, strength, min_strength, reasons)
        return True

    if _try_reverse_entry_side(entry_row, row, side, strength, reasons):
        reversed_row = _row_to_dict(entry_row)
        reversed_side = _norm_side(_first(reversed_row, ("side", "entry_decision", "ai_side"), side))
        if not _ma_structure_guard(reversed_row, reversed_side, symbol):
            if not _climax_exception(entry_row, reversed_row, reversed_side, symbol, "reverse_ma_structure"):
                return False
        if not _ma_deviation_guard(reversed_row, reversed_side, symbol):
            if not _climax_exception(entry_row, reversed_row, reversed_side, symbol, "reverse_ma_deviation"):
                return False
        return True

    if _climax_exception(entry_row, row, side, symbol, "direction_strength"):
        return True

    if not strict:
        weak = abs(strength) >= min_strength * 0.5
        if weak:
            logger.warning("[ENTRY DIRECTION CONFIRM] WEAK_ALLOW symbol=%s side=%s strength=%.3f min=%.3f reasons=%s", symbol, side, strength, min_strength, reasons)
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


def _is_currently_wrapped() -> bool:
    try:
        import trading.handlers.entry_controller as ec
        cur_atr = getattr(ec, "atr_1m_filter", None)
        cur_range = getattr(ec, "range_5m_filter", None)
        return bool(
            getattr(cur_atr, "_entry_direction_confirm_guard", False)
            and getattr(cur_range, "_entry_direction_confirm_guard", False)
        )
    except Exception:
        return False


def install() -> bool:
    global _INSTALLED, _ORIG_ATR_FILTER, _ORIG_RANGE_FILTER
    try:
        import trading.handlers.entry_controller as ec

        if _INSTALLED and _is_currently_wrapped():
            return True

        old_atr = getattr(ec, "atr_1m_filter", None)
        old_range = getattr(ec, "range_5m_filter", None)

        _ORIG_ATR_FILTER = old_atr
        _ORIG_RANGE_FILTER = old_range
        _patched_atr_1m_filter._entry_direction_confirm_guard = True  # type: ignore[attr-defined]
        _patched_range_5m_filter._entry_direction_confirm_guard = True  # type: ignore[attr-defined]
        ec.atr_1m_filter = _patched_atr_1m_filter
        ec.range_5m_filter = _patched_range_5m_filter
        _INSTALLED = True
        logger.warning(
            "[ENTRY DIRECTION CONFIRM] installed enabled=%s min_strength=%.3f strict=%s ma_guard=%s order_only_block=%s ma_dev_guard=%s climax_exception=%s buy_ma25_dev=%.3f buy_ma75_dev=%.3f sell_ma25_dev=%.3f sell_ma75_dev=%.3f reverse_enabled=%s reverse_min=%.3f reverse_half_size=%s",
            _env_bool("ENTRY_DIRECTION_CONFIRM_ENABLED", True),
            _env_float("ENTRY_DIRECTION_CONFIRM_MIN_STRENGTH", 1.5),
            _env_bool("ENTRY_DIRECTION_CONFIRM_STRICT", True),
            _env_bool("ENTRY_MA_STRUCTURE_GUARD_ENABLED", True),
            _env_bool("ENTRY_MA_ORDER_ONLY_BLOCK_WHEN_SLOPE_MISSING", True),
            _env_bool("ENTRY_MA_DEVIATION_GUARD_ENABLED", True),
            _env_bool("ENTRY_CLIMAX_REVERSAL_EXCEPTION_ENABLED", True),
            _env_float("ENTRY_BUY_MAX_NEGATIVE_MA25_DEV_PCT", -1.0),
            _env_float("ENTRY_BUY_MAX_NEGATIVE_MA75_DEV_PCT", -1.5),
            _env_float("ENTRY_SELL_MAX_POSITIVE_MA25_DEV_PCT", 1.0),
            _env_float("ENTRY_SELL_MAX_POSITIVE_MA75_DEV_PCT", 1.5),
            _env_bool("ENTRY_CONTRARIAN_REVERSE_ENABLED", True),
            _env_float("ENTRY_CONTRARIAN_REVERSE_MIN_STRENGTH", 2.5),
            _env_bool("ENTRY_CONTRARIAN_REVERSE_HALF_SIZE", True),
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
