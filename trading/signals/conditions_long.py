# ============================================================
# trading/signals/conditions_long.py
# Ver1.1-CONDITIONS-LONG-CLEAN
# ------------------------------------------------------------
# ✔ BUY（LONG）条件を完全網羅
# ✔ 1 condition = 1 reason（ini と完全一致）
# ✔ flag_builder_buy / add_scores と完全分離
# ✔ 計算ロジックのみ（flag参照禁止）
# ============================================================

import logging
import pandas as pd

logger = logging.getLogger(__name__)

# ============================================================
# 共通ユーティリティ
# ============================================================

def _is_bull(r):
    return r["close_price"] > r["open_price"]

def _is_bear(r):
    return r["close_price"] < r["open_price"]

def _body(r):
    return abs(r["close_price"] - r["open_price"])

def _range(r):
    return r["high_price"] - r["low_price"]


# ============================================================
# direction / MA
# ============================================================

def cond_dir_up(curr, prev, recent, *_):
    try:
        if curr.get("dir_up"):
            return True, "dir_up"
    except Exception:
        logger.exception("cond_dir_up")
    return False, None


def cond_ma_up(curr, prev, recent, *_):
    try:
        if curr["ma5"] > curr["ma25"]:
            return True, "ma_up"
    except Exception:
        pass
    return False, None


def cond_ma_alignment(curr, prev, recent, *_):
    try:
        if curr.get("ma_alignment"):
            return True, "ma_alignment"
    except Exception:
        pass
    return False, None


def cond_ma5_ma25_cross(curr, prev, recent, *_):
    try:
        if prev and prev["ma5"] <= prev["ma25"] and curr["ma5"] > curr["ma25"]:
            return True, "ma5_ma25_cross"
    except Exception:
        pass
    return False, None


def cond_perfect_order_event(curr, prev, recent, *_):
    try:
        if prev and prev.get("ma_alignment") and curr.get("ma_alignment"):
            return True, "perfect_order_event"
    except Exception:
        pass
    return False, None


# ============================================================
# momentum
# ============================================================

def cond_macd_gc(curr, prev, recent, *_):
    try:
        if curr.get("macd_gc"):
            return True, "macd_gc"
    except Exception:
        pass
    return False, None


def cond_rsi_rebound(curr, prev, recent, *_):
    try:
        if prev and prev["rsi"] < 30 and curr["rsi"] > prev["rsi"]:
            return True, "rsi_rebound"
    except Exception:
        pass
    return False, None


def cond_rci_trio_up(curr, prev, recent, *_):
    try:
        if recent is not None and "rci" in recent and len(recent) >= 3:
            r = recent["rci"].tail(3)
            if r.is_monotonic_increasing:
                return True, "rci_trio_up"
    except Exception:
        pass
    return False, None


def cond_rci9_uptrend(curr, prev, recent, *_):
    try:
        if curr.get("rci9_uptrend"):
            return True, "rci9_uptrend"
    except Exception:
        pass
    return False, None


# ============================================================
# price / volume
# ============================================================

def cond_vwap_break(curr, prev, recent, *_):
    try:
        if curr["close_price"] > curr["vwap"]:
            return True, "vwap_break"
    except Exception:
        pass
    return False, None


def cond_volume_surge(curr, prev, recent, *_):
    try:
        if recent is not None and len(recent) >= 5:
            avg = recent["volume"].iloc[-6:-1].mean()
            if avg > 0 and curr["volume"] > avg * 2:
                return True, "volume_surge"
    except Exception:
        pass
    return False, None


def cond_volume_price_breakout(curr, prev, recent, *_):
    try:
        if prev and recent is not None:
            avg = recent["volume"].iloc[-6:-1].mean()
            if curr["close_price"] > prev["close_price"] and curr["volume"] > avg * 1.5:
                return True, "volume_price_breakout"
    except Exception:
        pass
    return False, None


def cond_volume_zone_break(curr, prev, recent, *_):
    try:
        if recent is not None and len(recent) >= 30:
            zone = recent["high_price"].tail(30).quantile(0.7)
            if curr["close_price"] > zone:
                return True, "volume_zone_break"
    except Exception:
        pass
    return False, None


# ============================================================
# pullback / reversal
# ============================================================

def cond_first_pullback(curr, prev, recent, *_):
    try:
        if prev and curr["ma5"] > curr["ma25"]:
            touched = abs(curr["close_price"] - curr["ma25"]) / curr["ma25"] < 0.01
            if touched and prev["close_price"] > prev["ma25"]:
                return True, "first_pullback"
    except Exception:
        pass
    return False, None


def cond_rebound_on_ma25(curr, prev, recent, *_):
    try:
        if prev:
            touched = abs(prev["close_price"] - prev["ma25"]) / prev["ma25"] < 0.01
            if touched and curr["close_price"] > prev["close_price"]:
                return True, "rebound_on_ma25"
    except Exception:
        pass
    return False, None


def cond_fib_rebound(curr, prev, recent, *_):
    try:
        if recent is not None and len(recent) >= 20:
            hi = recent["high_price"].max()
            lo = recent["low_price"].min()
            fib50 = hi - (hi - lo) * 0.5
            if prev and prev["close_price"] < fib50 < curr["close_price"]:
                return True, "fib_rebound"
    except Exception:
        pass
    return False, None


# ============================================================
# bollinger
# ============================================================

def cond_bollinger_rebound(curr, prev, recent, *_):
    try:
        if curr["close_price"] > curr["bb_lower"]:
            return True, "bollinger_rebound"
    except Exception:
        pass
    return False, None


def cond_bb_3sigma_rebound(curr, prev, recent, *_):
    try:
        if curr.get("bb_lower_3sigma") and curr["close_price"] > curr["bb_lower_3sigma"]:
            return True, "bb_3sigma_rebound"
    except Exception:
        pass
    return False, None


# ============================================================
# candle patterns（BUY）
# ============================================================

def cond_bullish_engulfing(curr, prev, *_):
    try:
        if prev and _is_bear(prev) and _is_bull(curr):
            if curr["open_price"] < prev["close_price"] and curr["close_price"] > prev["open_price"]:
                return True, "bullish_engulfing"
    except Exception:
        pass
    return False, None


def cond_bullish_counterattack(curr, prev, *_):
    try:
        if prev and _is_bear(prev) and _is_bull(curr):
            if abs(curr["close_price"] - prev["close_price"]) / prev["close_price"] < 0.002:
                return True, "bullish_counterattack"
    except Exception:
        pass
    return False, None


def cond_bullish_side_by_side(curr, prev, *_):
    try:
        if prev and _is_bull(prev) and _is_bull(curr):
            if abs(curr["close_price"] - prev["close_price"]) / prev["close_price"] < 0.003:
                return True, "bullish_side_by_side"
    except Exception:
        pass
    return False, None


def cond_bullish_mat_hold(curr, prev, recent, *_):
    try:
        if recent is not None and len(recent) >= 5:
            seq = recent.tail(5)
            if _is_bull(seq.iloc[0]) and all(_is_bear(seq.iloc[i]) for i in range(1, 4)) and _is_bull(seq.iloc[4]):
                return True, "bullish_mat_hold"
    except Exception:
        pass
    return False, None


def cond_bullish_belt_hold(curr, *_):
    try:
        if _is_bull(curr) and abs(curr["open_price"] - curr["low_price"]) / curr["low_price"] < 0.001:
            return True, "bullish_belt_hold"
    except Exception:
        pass
    return False, None


def cond_bullish_harami(curr, prev, *_):
    try:
        if prev and _is_bear(prev) and _is_bull(curr):
            if curr["high_price"] < prev["high_price"] and curr["low_price"] > prev["low_price"]:
                return True, "bullish_harami"
    except Exception:
        pass
    return False, None


def cond_bullish_breakaway(curr, prev, recent, *_):
    try:
        if recent is not None and len(recent) >= 5:
            if _is_bear(recent.iloc[-5]) and _is_bull(curr):
                return True, "bullish_breakaway"
    except Exception:
        pass
    return False, None


def cond_bullish_kicker(curr, prev, *_):
    try:
        if prev and _is_bear(prev) and _is_bull(curr) and curr["open_price"] > prev["open_price"]:
            return True, "bullish_kicker"
    except Exception:
        pass
    return False, None


def cond_bullish_tweezer_bottom(curr, prev, *_):
    try:
        if prev and abs(curr["low_price"] - prev["low_price"]) / prev["low_price"] < 0.001:
            if _is_bear(prev) and _is_bull(curr):
                return True, "bullish_tweezer_bottom"
    except Exception:
        pass
    return False, None


def cond_morning_star(curr, prev, recent, *_):
    try:
        if recent is not None and len(recent) >= 3:
            a, b, c = recent.tail(3).itertuples(index=False)
            if _is_bear(a) and _body(b) < _body(a) * 0.5 and _is_bull(c):
                return True, "morning_star"
    except Exception:
        pass
    return False, None


def cond_piercing_line(curr, prev, *_):
    try:
        if prev and _is_bear(prev) and _is_bull(curr):
            mid = (prev["open_price"] + prev["close_price"]) / 2
            if curr["close_price"] > mid:
                return True, "piercing_line"
    except Exception:
        pass
    return False, None


def cond_hammer(curr, *_):
    try:
        body = _body(curr)
        lower = min(curr["open_price"], curr["close_price"]) - curr["low_price"]
        if lower > body * 2:
            return True, "hammer"
    except Exception:
        pass
    return False, None


def cond_inverted_hammer(curr, *_):
    try:
        body = _body(curr)
        upper = curr["high_price"] - max(curr["open_price"], curr["close_price"])
        if upper > body * 2:
            return True, "inverted_hammer"
    except Exception:
        pass
    return False, None


def cond_dragonfly_doji(curr, *_):
    try:
        if abs(curr["open_price"] - curr["close_price"]) / curr["close_price"] < 0.001:
            if curr["high_price"] - curr["close_price"] < _range(curr) * 0.1:
                return True, "dragonfly_doji"
    except Exception:
        pass
    return False, None


def cond_rising_three_methods(curr, prev, recent, *_):
    try:
        if recent is not None and len(recent) >= 5:
            seq = recent.tail(5)
            if _is_bull(seq.iloc[0]) and _is_bull(seq.iloc[-1]):
                return True, "rising_three_methods"
    except Exception:
        pass
    return False, None


def cond_window_up(curr, prev, *_):
    try:
        if prev and curr["low_price"] > prev["high_price"]:
            return True, "window_up"
    except Exception:
        pass
    return False, None


def cond_gap_up_breakout(curr, prev, *_):
    try:
        if prev and curr["open_price"] > prev["high_price"]:
            return True, "gap_up_breakout"
    except Exception:
        pass
    return False, None


def cond_bull_big_combo(curr, prev, recent, *_):
    try:
        ok1, _ = cond_bullish_engulfing(curr, prev)
        ok2, _ = cond_volume_surge(curr, prev, recent)
        if ok1 and ok2:
            return True, "bull_big_combo"
    except Exception:
        pass
    return False, None


def cond_lower_wick_low_zone(curr, *_):
    try:
        body = _body(curr)
        lower = min(curr["open_price"], curr["close_price"]) - curr["low_price"]
        if lower > body * 1.5 and _is_bull(curr):
            return True, "lower_wick_low_zone"
    except Exception:
        pass
    return False, None


def cond_lower_wick_rebound(curr, *_):
    try:
        body = _body(curr)
        lower = min(curr["open_price"], curr["close_price"]) - curr["low_price"]
        if lower > body * 1.5 and _is_bull(curr):
            return True, "lower_wick_rebound"
    except Exception:
        pass
    return False, None


# ============================================================
# BUY 条件一覧
# ============================================================

conditions_long = [
    # direction / MA
    cond_dir_up,
    cond_ma_up,
    cond_ma_alignment,
    cond_ma5_ma25_cross,
    cond_perfect_order_event,

    # momentum
    cond_macd_gc,
    cond_rsi_rebound,
    cond_rci_trio_up,
    cond_rci9_uptrend,

    # price / volume
    cond_vwap_break,
    cond_volume_surge,
    cond_volume_price_breakout,
    cond_volume_zone_break,

    # pullback / reversal
    cond_first_pullback,
    cond_rebound_on_ma25,
    cond_fib_rebound,

    # bollinger
    cond_bollinger_rebound,
    cond_bb_3sigma_rebound,

    # candle patterns
    cond_bullish_engulfing,
    cond_bullish_counterattack,
    cond_bullish_side_by_side,
    cond_bullish_mat_hold,
    cond_bullish_belt_hold,
    cond_bullish_harami,
    cond_bullish_breakaway,
    cond_bullish_kicker,
    cond_bullish_tweezer_bottom,

    cond_morning_star,
    cond_piercing_line,
    cond_hammer,
    cond_inverted_hammer,
    cond_dragonfly_doji,
    cond_rising_three_methods,
    cond_window_up,
    cond_gap_up_breakout,

    # strong combo
    cond_bull_big_combo,
    cond_lower_wick_low_zone,
    cond_lower_wick_rebound,
]
