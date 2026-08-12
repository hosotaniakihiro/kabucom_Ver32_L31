# ============================================================
# File   : trading/entry/direction_confirm.py
# ------------------------------------------------------------
# エントリー直前の方向確認ガード。
#
# 旧構成 (core/startup 配下の 4 パッチ) を本文化したもの:
#   1. entry_direction_confirm_guard_patch  Ver07-NO-WRAP-RECURSION-FIX
#        判定関数の本体 (pure guard)。monkeypatch はしない純ライブラリだった。
#   2. ma_cross_state_runtime_patch         Ver01
#        _calc_direction_strength をラップし MA クロス継続状態を加点。
#   3. vwap_state_runtime_patch             Ver01
#        _calc_direction_strength をラップし VWAP 継続状態を加点。
#   4. entry_direction_failopen_runtime_patch Ver05-NO-REVERSE-WHEN-SCORE-DOMINANT
#        check_entry_direction_confirm をラップし、SUMMARY_AI 候補の
#        fail-open / 逆トレンド時の side 反転を行う。
#
# 旧構成の合成順 (main.py:215 low_movement_entry_guard_patch →
# main.py:217 oneshot_limit_700k_patch) を、そのまま直列ステージとして再現する:
#   _calc_direction_strength      = vwap( ma_cross( core ) )
#   check_entry_direction_confirm = failopen( core )
#
# 注意:
#   ATR/RANGE フィルタの monkey patch はここでは行わない
#   (low_movement_entry_guard_patch と相互再帰するため旧版で廃止済み)。
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


# ============================================================
# 共通ヘルパ
# ============================================================

def _env_bool(name: str, default: bool = True) -> bool:
    """pure guard / ma_cross / vwap 用。空文字は False 扱い。"""
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


def _failopen_env_bool(name: str, default: bool = True) -> bool:
    """fail-open レイヤ用。空文字は default 扱いで、受理する語彙も広い。

    pure guard 側の _env_bool とは空文字の扱いが違うので共用しないこと
    (旧 entry_direction_failopen_runtime_patch._env_bool の忠実な移設)。
    """
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        s = str(v).strip().lower()
        if s in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}:
            return True
        if s in {"0", "false", "no", "n", "off", "ng", "disable", "disabled"}:
            return False
        return bool(default)
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
        if entry_row is None:
            return
        if isinstance(entry_row, dict):
            entry_row[key] = value
            return
        if hasattr(entry_row, "__setitem__"):
            entry_row[key] = value
    except Exception:
        return


# ============================================================
# pure guard: MA 構造 / MA 乖離 / クライマックス例外
# ============================================================

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

    except Exception as e:
        logger.warning(
            "[ENTRY CLIMAX EXCEPTION] failed symbol=%s side=%s blocked_by=%s err=%s",
            symbol, side, blocked_by, e,
            exc_info=False,
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


# ============================================================
# 方向強度
#   core → MA クロス継続状態 → VWAP 継続状態 の直列合成。
#   旧構成の install 順 (low_movement_entry_guard_patch:887 ma_cross →
#   :888 vwap) がそのまま「vwap が最外周」だったので同じ順序にしてある。
# ============================================================

def _calc_direction_strength_core(row: dict) -> tuple[float, list[str]]:
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


def _normalize_strength(strength: Any, reasons: Any) -> tuple[float, list[str]]:
    try:
        return _safe_float(strength, 0.0), list(reasons or [])
    except Exception:
        return 0.0, []


def _apply_ma_cross_state(strength: float, reasons: list[str], row: dict) -> tuple[float, list[str]]:
    """旧 core/startup/ma_cross_state_runtime_patch.py。

    ゴールデンクロス/デッドクロスの一瞬だけでなく、その後 ma5 > ma25 /
    ma5 < ma25 が継続している状態を方向強度へ加算する。
    """
    strength, reasons = _normalize_strength(strength, reasons)

    if not _env_bool("ENTRY_MA_CROSS_STATE_ENABLED", True):
        return strength, reasons

    try:
        from trading.entry.ma_cross_state_detector import detect_ma_cross_state
        res = detect_ma_cross_state(row)
        delta = _safe_float(res.get("score_delta"), 0.0)
        state = str(res.get("state") or "")
        ma_reasons = res.get("reasons") or []
        if delta != 0.0:
            strength += delta
            reasons.append(f"ma_cross_state:{state}:{delta:+.2f}")
            for r in ma_reasons[:5]:
                reasons.append(f"ma_cross_{r}")
        logger.info(
            "[MA CROSS STATE PATCH] state=%s delta=%.3f strength_after=%.3f reasons=%s",
            state,
            delta,
            strength,
            ma_reasons,
        )
    except Exception:
        logger.exception("[MA CROSS STATE PATCH] detect/apply failed")
        reasons.append("ma_cross_state_error")

    return float(strength), reasons


def _apply_vwap_state(strength: float, reasons: list[str], row: dict) -> tuple[float, list[str]]:
    """旧 core/startup/vwap_state_runtime_patch.py。

    VWAP 上抜け/下抜けの一瞬だけでなく、その後 close > vwap / close < vwap が
    継続している状態を方向強度へ加算する。
    """
    strength, reasons = _normalize_strength(strength, reasons)

    if not _env_bool("ENTRY_VWAP_STATE_ENABLED", True):
        return strength, reasons

    try:
        from trading.entry.vwap_state_detector import detect_vwap_state
        res = detect_vwap_state(row)
        delta = _safe_float(res.get("score_delta"), 0.0)
        state = str(res.get("state") or "")
        vwap_reasons = res.get("reasons") or []
        if delta != 0.0:
            strength += delta
            reasons.append(f"vwap_state:{state}:{delta:+.2f}")
            for r in vwap_reasons[:5]:
                reasons.append(f"vwap_{r}")
        logger.info(
            "[VWAP STATE PATCH] state=%s delta=%.3f strength_after=%.3f reasons=%s",
            state,
            delta,
            strength,
            vwap_reasons,
        )
    except Exception:
        logger.exception("[VWAP STATE PATCH] detect/apply failed")
        reasons.append("vwap_state_error")

    return float(strength), reasons


def _calc_direction_strength(row: dict) -> tuple[float, list[str]]:
    strength, reasons = _calc_direction_strength_core(row)
    strength, reasons = _apply_ma_cross_state(strength, reasons, row)
    strength, reasons = _apply_vwap_state(strength, reasons, row)
    return strength, reasons


# ============================================================
# pure guard 本体
# ============================================================

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


def _check_entry_direction_confirm_core(entry_row: Any = None, *args, **kwargs) -> bool:
    """pure guard の判定入口 (旧 entry_direction_confirm_guard_patch の公開関数)。"""
    # 旧 core/startup/entry_direction_failclosed_patch.py (V1.1) はこの関数を
    # 外側からラップし、RecursionError を ENTRY_DIRECTION_CONFIRM_RECURSION_FAIL_OPEN、
    # 一般例外を ENTRY_DIRECTION_CONFIRM_ERROR_FAIL_OPEN で fail-open させる意図だった。
    # しかし下の try/except が先に両方を捕まえて False を返すため、外側ラッパーの
    # except には一度も到達しない = 完全な no-op だった (env にも他の読み手なし)。
    # 挙動を変えないため env は配線せず、fail-safe NG のままにしてある。
    # fail-open を本当に有効にしたい場合は、ここを env 判定へ変えたうえで
    # entry_final_filter_failopen_patch:196 の
    # ENTRY_DIRECTION_CONFIRM_RECURSION_FAIL_OPEN='1' が効くようになる点に注意すること。
    if entry_row is None:
        return True
    try:
        return bool(_direction_confirm(entry_row))
    except RecursionError:
        logger.error(
            "[ENTRY DIRECTION CONFIRM] recursion detected in pure guard. fail-safe NG.",
            exc_info=False,
        )
        return False
    except Exception as e:
        logger.warning(
            "[ENTRY DIRECTION CONFIRM] pure guard failed: %s",
            e,
            exc_info=False,
        )
        return False


# ============================================================
# fail-open レイヤ
#   旧 core/startup/entry_direction_failopen_runtime_patch.py Ver05。
#   SUMMARY_AI 候補が pure guard で NG / RecursionError になったとき、
#   明確な逆トレンドなら side を反転し、そうでなければ通す。
# ============================================================

def _failopen_opposite_side(side: str) -> str:
    """fail-open レイヤ用。_norm_side を通さない (旧パッチの忠実な移設)。"""
    side = str(side or "").upper()
    if side == "SELL":
        return "BUY"
    if side == "BUY":
        return "SELL"
    return side


def _is_summary_ai(row: Any) -> bool:
    d = _row_to_dict(row)
    src = str(d.get("source") or "").upper()
    et = str(d.get("entry_type") or "").upper()
    return src == "SUMMARY" or et == "SUMMARY_AI"


def _symbol(row: Any) -> str:
    d = _row_to_dict(row)
    s = str(d.get("symbol") or d.get("code") or d.get("stock_code") or "").strip()
    if s.endswith(".0") and s[:-2].isdigit():
        return s[:-2]
    return s


def _side(row: Any) -> str:
    d = _row_to_dict(row)
    s = str(d.get("side") or d.get("entry_decision") or d.get("ai_side") or "").strip().upper()
    if s in {"1", "SELL", "SHORT", "売", "売り"}:
        return "SELL"
    if s in {"2", "BUY", "LONG", "買", "買い"}:
        return "BUY"
    return s


def _trend_diag(row: Any) -> dict[str, Any]:
    d = _row_to_dict(row)
    close = _safe_float(_first(d, ("close", "close_price", "price", "current_price"), 0.0), 0.0)
    open_ = _safe_float(_first(d, ("open", "open_price"), 0.0), 0.0)
    high = _safe_float(_first(d, ("high", "high_price"), 0.0), 0.0)
    low = _safe_float(_first(d, ("low", "low_price"), 0.0), 0.0)
    ma5 = _safe_float(_first(d, ("ma5", "MA5", "ma_5", "daily_ma5", "MA_5"), 0.0), 0.0)
    ma25 = _safe_float(_first(d, ("ma25", "MA25", "ma_25", "daily_ma25", "MA_25"), 0.0), 0.0)
    ma75 = _safe_float(_first(d, ("ma75", "MA75", "ma_75", "daily_ma75", "MA_75"), 0.0), 0.0)
    slope = _safe_float(_first(d, ("slope_atr_scaled", "score_slope", "disp_slope", "slope"), 0.0), 0.0)
    mtf = _safe_float(_first(d, ("score_mtf", "mtf_score", "disp_mtf", "mtf"), 0.0), 0.0)
    macd = _safe_float(d.get("macd"), 0.0)
    signal = _safe_float(d.get("signal"), 0.0)
    hist = _safe_float(d.get("hist"), macd - signal)
    buy_score = _safe_float(_first(d, ("score_buy", "buy_score", "disp_buy_score", "buy"), 0.0), 0.0)
    sell_score = _safe_float(_first(d, ("score_sell", "sell_score", "disp_sell_score", "sell"), 0.0), 0.0)

    macd_diff = hist if hist != 0 else macd - signal

    # 同値 close==ma5==ma25==ma75 を bullish/bearish 両方にしない。
    ma_bull = bool(close > 0 and ma5 > 0 and ma25 > 0 and ma75 > 0 and close > ma5 and ma5 >= ma25 >= ma75)
    ma_bear = bool(close > 0 and ma5 > 0 and ma25 > 0 and ma75 > 0 and close < ma5 and ma5 <= ma25 <= ma75)
    price_above_ma = bool(close > 0 and ((ma25 > 0 and close > ma25) or (ma75 > 0 and close > ma75)))
    price_below_ma = bool(close > 0 and ((ma25 > 0 and close < ma25) or (ma75 > 0 and close < ma75)))
    bar_green = bool(close > 0 and open_ > 0 and close > open_)
    bar_red = bool(close > 0 and open_ > 0 and close < open_)
    near_high = bool(high > low and close > 0 and ((close - low) / max(high - low, 1e-9)) >= 0.65)
    near_low = bool(high > low and close > 0 and ((close - low) / max(high - low, 1e-9)) <= 0.35)
    slope_up = slope > 0
    slope_down = slope < 0
    mtf_up = mtf > 0
    mtf_down = mtf < 0
    macd_up = macd_diff > 0
    macd_down = macd_diff < 0
    buy_dominant = buy_score > sell_score and buy_score > 0
    sell_dominant = sell_score > buy_score and sell_score > 0

    bullish_points = sum(bool(x) for x in (ma_bull, price_above_ma, bar_green, near_high, slope_up, mtf_up, macd_up, buy_dominant))
    bearish_points = sum(bool(x) for x in (ma_bear, price_below_ma, bar_red, near_low, slope_down, mtf_down, macd_down, sell_dominant))

    return {
        "close": close,
        "open": open_,
        "ma5": ma5,
        "ma25": ma25,
        "ma75": ma75,
        "slope": slope,
        "mtf": mtf,
        "macd": macd,
        "signal": signal,
        "hist": hist,
        "buy_score": buy_score,
        "sell_score": sell_score,
        "ma_bull": ma_bull,
        "ma_bear": ma_bear,
        "price_above_ma": price_above_ma,
        "price_below_ma": price_below_ma,
        "bar_green": bar_green,
        "bar_red": bar_red,
        "near_high": near_high,
        "near_low": near_low,
        "slope_up": slope_up,
        "slope_down": slope_down,
        "mtf_up": mtf_up,
        "mtf_down": mtf_down,
        "macd_up": macd_up,
        "macd_down": macd_down,
        "buy_dominant": buy_dominant,
        "sell_dominant": sell_dominant,
        "bullish_points": bullish_points,
        "bearish_points": bearish_points,
    }


def _score_supports_current_side(side: str, diag: dict[str, Any]) -> bool:
    """AI/score が明確に現在sideを支持しているなら、方向ガードで反転しない。"""
    if not _failopen_env_bool("ENTRY_DIRECTION_KEEP_SCORE_DOMINANT_SIDE", True):
        return False
    side_u = str(side or "").upper()
    buy_score = _safe_float(diag.get("buy_score"), 0.0)
    sell_score = _safe_float(diag.get("sell_score"), 0.0)
    min_score = _env_float("ENTRY_DIRECTION_KEEP_MIN_SCORE", 0.000001)
    margin = _env_float("ENTRY_DIRECTION_KEEP_SCORE_MARGIN", 0.0)
    if side_u == "SELL":
        return sell_score >= min_score and sell_score >= buy_score + margin
    if side_u == "BUY":
        return buy_score >= min_score and buy_score >= sell_score + margin
    return False


def _is_bullish_trend_for_sell(row: Any, diag: dict[str, Any] | None = None) -> bool:
    if not _failopen_env_bool("ENTRY_BLOCK_SELL_IN_BULLISH_TREND", True):
        return False
    diag = diag or _trend_diag(row)
    if _score_supports_current_side("SELL", diag):
        return False
    min_points = max(1, _env_int("ENTRY_TREND_BLOCK_MIN_POINTS", 2))
    gap = _env_int("ENTRY_TREND_BLOCK_POINT_GAP", 1)
    bull = int(diag.get("bullish_points") or 0)
    bear = int(diag.get("bearish_points") or 0)
    return bull >= min_points and bull >= bear + gap


def _is_bearish_trend_for_buy(row: Any, diag: dict[str, Any] | None = None) -> bool:
    if not _failopen_env_bool("ENTRY_BLOCK_BUY_IN_BEARISH_TREND", True):
        return False
    diag = diag or _trend_diag(row)
    if _score_supports_current_side("BUY", diag):
        return False
    min_points = max(1, _env_int("ENTRY_TREND_BLOCK_MIN_POINTS", 2))
    gap = _env_int("ENTRY_TREND_BLOCK_POINT_GAP", 1)
    bull = int(diag.get("bullish_points") or 0)
    bear = int(diag.get("bearish_points") or 0)
    return bear >= min_points and bear >= bull + gap


def _against_clear_trend(row: Any) -> tuple[bool, str, dict[str, Any]]:
    side = _side(row)
    diag = _trend_diag(row)
    if side == "SELL" and _is_bullish_trend_for_sell(row, diag):
        return True, "sell_in_bullish_trend", diag
    if side == "BUY" and _is_bearish_trend_for_buy(row, diag):
        return True, "buy_in_bearish_trend", diag
    return False, "", diag


def _allow_failopen_for_side(row: Any) -> bool:
    side = _side(row)
    if side == "BUY":
        return _failopen_env_bool("ENTRY_DIRECTION_FAILOPEN_FOR_SUMMARY_AI_BUY", True)
    if side == "SELL":
        return _failopen_env_bool("ENTRY_DIRECTION_FAILOPEN_FOR_SUMMARY_AI_SELL", True)
    return False


def _reverse_against_trend(row: Any, reason: str, against_reason: str, diag: dict[str, Any]) -> bool:
    if not _failopen_env_bool("ENTRY_DIRECTION_REVERSE_AGAINST_CLEAR_TREND", True):
        return False

    if not _is_summary_ai(row):
        return False

    old_side = _side(row)
    if _score_supports_current_side(old_side, diag):
        logger.warning(
            "[ENTRY DIRECTION SAFETY] KEEP score-dominant side symbol=%s side=%s against=%s reason=%s diag=%s",
            _symbol(row), old_side, against_reason, reason, diag,
        )
        return False

    new_side = _failopen_opposite_side(old_side)
    if new_side not in {"BUY", "SELL"} or new_side == old_side:
        return False

    symbol = _symbol(row)
    _set_row_value(row, "original_side", old_side)
    _set_row_value(row, "side", new_side)
    _set_row_value(row, "entry_decision", new_side)
    _set_row_value(row, "ai_side", new_side)
    _set_row_value(row, "direction_safety_reversed", True)
    _set_row_value(row, "reverse_reason", against_reason)
    _set_row_value(row, "reverse_source_reason", reason)
    _set_row_value(row, "direction_safety_diag", str(diag))

    if _failopen_env_bool("ENTRY_DIRECTION_REVERSE_HALF_SIZE", True):
        for key in ("lot_multiplier", "qty_multiplier"):
            try:
                cur = _safe_float(_row_to_dict(row).get(key), 1.0)
                if cur > 0:
                    _set_row_value(row, key, max(0.5, cur * 0.5))
            except Exception:
                pass
        _set_row_value(row, "reverse_half_size", True)

    logger.warning(
        "[ENTRY DIRECTION SAFETY] REVERSE symbol=%s %s->%s against=%s reason=%s diag=%s",
        symbol,
        old_side,
        new_side,
        against_reason,
        reason,
        diag,
    )
    return True


def _maybe_failopen(row: Any, reason: str) -> bool:
    side = _side(row)
    symbol = _symbol(row)

    if not _is_summary_ai(row):
        return False

    against, against_reason, diag = _against_clear_trend(row)
    if against:
        if _score_supports_current_side(side, diag):
            logger.warning(
                "[ENTRY DIRECTION SAFETY] KEEP SUMMARY_AI score-dominant side symbol=%s side=%s against=%s reason=%s diag=%s",
                symbol, side, against_reason, reason, diag,
            )
            return True
        if _reverse_against_trend(row, reason, against_reason, diag):
            return True
        logger.warning(
            "[ENTRY DIRECTION SAFETY] BLOCK %s symbol=%s side=%s reason=%s diag=%s",
            against_reason,
            symbol,
            side,
            reason,
            diag,
        )
        return False

    if not _allow_failopen_for_side(row):
        logger.warning(
            "[ENTRY DIRECTION SAFETY] BLOCK fail-open disabled symbol=%s side=%s reason=%s diag=%s",
            symbol,
            side,
            reason,
            diag,
        )
        return False

    logger.warning(
        "[ENTRY DIRECTION FAILOPEN] allow SUMMARY_AI symbol=%s side=%s reason=%s diag=%s",
        symbol,
        side,
        reason,
        diag,
    )
    return True


def check_entry_direction_confirm(entry_row: Any = None, *args, **kwargs) -> bool:
    """エントリー直前の方向確認 (pure guard + fail-open レイヤの合成)。

    low_movement_entry_guard_patch の単一ラッパーから呼ばれる公開関数。
    ここでは元 ATR/RANGE フィルタを呼ばないため、二重パッチ再帰が発生しない。
    """
    try:
        ok = bool(_check_entry_direction_confirm_core(entry_row, *args, **kwargs))
        if ok:
            against, against_reason, diag = _against_clear_trend(entry_row)
            if _is_summary_ai(entry_row) and against:
                side = _side(entry_row)
                if _score_supports_current_side(side, diag):
                    logger.warning(
                        "[ENTRY DIRECTION SAFETY] KEEP despite trend diag because score supports current side symbol=%s side=%s against=%s diag=%s",
                        _symbol(entry_row), side, against_reason, diag,
                    )
                    return True
                if _reverse_against_trend(entry_row, "original_guard_ok_but_against_trend", against_reason, diag):
                    return True
                logger.warning(
                    "[ENTRY DIRECTION SAFETY] BLOCK despite original OK %s symbol=%s side=%s diag=%s",
                    against_reason,
                    _symbol(entry_row),
                    _side(entry_row),
                    diag,
                )
                return False
            return True

        return _maybe_failopen(entry_row, "original_guard_ng")

    except RecursionError:
        return _maybe_failopen(entry_row, "recursion_error")

    except Exception as e:
        logger.warning("[ENTRY DIRECTION SAFETY] direction guard error: %s", e, exc_info=False)
        return _maybe_failopen(entry_row, f"guard_error:{e}")


__all__ = ["check_entry_direction_confirm"]
