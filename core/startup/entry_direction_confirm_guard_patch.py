# ============================================================
# File   : core/startup/entry_direction_confirm_guard_patch.py
# Version: Ver03-MA-STRUCTURE-HARD-GUARD
# ------------------------------------------------------------
# 「売ったら上がる / 買ったら下がる」を減らすため、
# エントリー直前で方向確認を行う runtime patch。
#
# Ver02:
#   通常シグナルと実方向が強く逆なら、条件付きで反転する。
#
# Ver03:
#   分足MA構造の最終ガードを追加。
#
#   BUY禁止:
#     - close < ma5 / ma25 / ma75
#     - ma5 <= ma25 <= ma75
#     - ma25下向き or ma75下向き
#
#   SELL禁止:
#     - close > ma5 / ma25 / ma75
#     - ma5 >= ma25 >= ma75
#     - ma25上向き or ma75上向き
#
#   目的:
#     名村造船所のように、分足移動平均が全部下向きで
#     株価もその下にあるのにBUYしてしまう事故を止める。
#
# 環境変数:
#   ENTRY_DIRECTION_CONFIRM_ENABLED=1
#   ENTRY_DIRECTION_CONFIRM_MIN_STRENGTH=1.5
#   ENTRY_DIRECTION_CONFIRM_STRICT=1
#
#   ENTRY_MA_STRUCTURE_GUARD_ENABLED=1
#   ENTRY_MA_REQUIRE_PRICE_ABOVE_ALL_FOR_BUY=1
#   ENTRY_MA_REQUIRE_PRICE_BELOW_ALL_FOR_SELL=1
#
#   ENTRY_CONTRARIAN_REVERSE_ENABLED=1
#   ENTRY_CONTRARIAN_REVERSE_MIN_STRENGTH=2.5
#   ENTRY_CONTRARIAN_REVERSE_HALF_SIZE=1
#
# ログ:
#   [ENTRY MA STRUCTURE GUARD] NG ...
#   [ENTRY DIRECTION CONFIRM] OK/NG ...
#   [ENTRY CONTRARIAN REVERSE] BUY->SELL / SELL->BUY ...
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


def _get_price_ma(row: dict) -> tuple[float, float, float, float]:
    close = _safe_float(_first(row, ("close", "close_price", "price", "current_price"), 0.0), 0.0)
    ma5 = _safe_float(_first(row, ("ma5", "MA5", "ma_5"), 0.0), 0.0)
    ma25 = _safe_float(_first(row, ("ma25", "MA25", "ma_25"), 0.0), 0.0)
    ma75 = _safe_float(_first(row, ("ma75", "MA75", "ma_75"), 0.0), 0.0)
    return close, ma5, ma25, ma75


def _get_ma_slope(row: dict, ma_key: str, fallback_slope: float = 0.0) -> float:
    keys = (
        f"{ma_key}_slope",
        f"{ma_key}_Slope",
        f"{ma_key.upper()}_Slope",
        f"{ma_key.upper()}_slope",
    )
    return _safe_float(_first(row, keys, fallback_slope), fallback_slope)


def _ma_structure_guard(row: dict, side: str, symbol: str) -> bool:
    """
    MA構造による最終ガード。

    BUY:
      株価が ma5/ma25/ma75 をすべて下回り、かつ ma5<=ma25<=ma75 の
      下落パーフェクトオーダーなら禁止。

    SELL:
      株価が ma5/ma25/ma75 をすべて上回り、かつ ma5>=ma25>=ma75 の
      上昇パーフェクトオーダーなら禁止。
    """
    if not _env_bool("ENTRY_MA_STRUCTURE_GUARD_ENABLED", True):
        return True

    close, ma5, ma25, ma75 = _get_price_ma(row)
    if close <= 0 or ma5 <= 0 or ma25 <= 0 or ma75 <= 0:
        logger.info(
            "[ENTRY MA STRUCTURE GUARD] SKIP symbol=%s side=%s reason=ma_missing close=%.4f ma5=%.4f ma25=%.4f ma75=%.4f",
            symbol, side, close, ma5, ma25, ma75,
        )
        return True

    # 分足側にMA傾きが無いケースがあるため、ma5/25/75 の並びも傾き代理として使う。
    fallback = _safe_float(_first(row, ("slope", "score_slope", "slope_atr_scaled"), 0.0), 0.0)
    ma25_slope = _get_ma_slope(row, "ma25", fallback)
    ma75_slope = _get_ma_slope(row, "ma75", fallback)

    bearish_price = close < ma5 and close < ma25 and close < ma75
    bearish_order = ma5 <= ma25 <= ma75
    bearish_ma_slope = ma25_slope <= 0 or ma75_slope <= 0

    bullish_price = close > ma5 and close > ma25 and close > ma75
    bullish_order = ma5 >= ma25 >= ma75
    bullish_ma_slope = ma25_slope >= 0 or ma75_slope >= 0

    if side == "BUY":
        if _env_bool("ENTRY_MA_REQUIRE_PRICE_ABOVE_ALL_FOR_BUY", True):
            if bearish_price and bearish_order and bearish_ma_slope:
                logger.warning(
                    "[ENTRY MA STRUCTURE GUARD] NG symbol=%s side=BUY reason=bearish_ma_structure close=%.4f ma5=%.4f ma25=%.4f ma75=%.4f ma25_slope=%.6f ma75_slope=%.6f",
                    symbol, close, ma5, ma25, ma75, ma25_slope, ma75_slope,
                )
                return False

    if side == "SELL":
        if _env_bool("ENTRY_MA_REQUIRE_PRICE_BELOW_ALL_FOR_SELL", True):
            if bullish_price and bullish_order and bullish_ma_slope:
                logger.warning(
                    "[ENTRY MA STRUCTURE GUARD] NG symbol=%s side=SELL reason=bullish_ma_structure close=%.4f ma5=%.4f ma25=%.4f ma75=%.4f ma25_slope=%.6f ma75_slope=%.6f",
                    symbol, close, ma5, ma25, ma75, ma25_slope, ma75_slope,
                )
                return False

    logger.info(
        "[ENTRY MA STRUCTURE GUARD] OK symbol=%s side=%s close=%.4f ma5=%.4f ma25=%.4f ma75=%.4f ma25_slope=%.6f ma75_slope=%.6f",
        symbol, side, close, ma5, ma25, ma75, ma25_slope, ma75_slope,
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

    close2, ma5, ma25, ma75 = _get_price_ma(row)
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


def _set_row_value(entry_row: Any, key: str, value: Any) -> None:
    try:
        if isinstance(entry_row, dict):
            entry_row[key] = value
            return
        if hasattr(entry_row, "__setitem__"):
            entry_row[key] = value
    except Exception:
        return


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
        return False

    strength, reasons = _calc_direction_strength(row)

    if side == "BUY":
        ok = strength >= min_strength
    else:
        ok = strength <= -min_strength

    if ok:
        logger.info("[ENTRY DIRECTION CONFIRM] OK symbol=%s side=%s strength=%.3f min=%.3f reasons=%s", symbol, side, strength, min_strength, reasons)
        return True

    if _try_reverse_entry_side(entry_row, row, side, strength, reasons):
        # 反転後のMA構造も必ず確認する。
        reversed_side = _norm_side(_first(_row_to_dict(entry_row), ("side", "entry_decision", "ai_side"), side))
        if _ma_structure_guard(_row_to_dict(entry_row), reversed_side, symbol):
            return True
        return False

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

        # 他パッチが後から filter を上書きした場合に再ラップする。
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
            "[ENTRY DIRECTION CONFIRM] installed enabled=%s min_strength=%.3f strict=%s ma_guard=%s reverse_enabled=%s reverse_min=%.3f reverse_half_size=%s",
            _env_bool("ENTRY_DIRECTION_CONFIRM_ENABLED", True),
            _env_float("ENTRY_DIRECTION_CONFIRM_MIN_STRENGTH", 1.5),
            _env_bool("ENTRY_DIRECTION_CONFIRM_STRICT", True),
            _env_bool("ENTRY_MA_STRUCTURE_GUARD_ENABLED", True),
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
