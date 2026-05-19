# ============================================================
# File   : trading/entry/climax_reversal_detector.py
# Version: Ver03-MA-DEVIATION-REVERSAL-CONTRARIAN-GUARD
# ------------------------------------------------------------
# 分足レベルの逆張りクライマックス反転検出。
#
# 重要方針:
#   逆張りエントリーは、単なるヒゲ・出来高急増だけでは許可しない。
#
# BUY逆張りを許可する条件:
#   1. 長い間、株価が75MAの下で推移している、または現在75MA下
#   2. 25MAと75MAが収束してきている、または現在ギャップが小さい
#   3. MA25/MA75から下方乖離している
#   4. 出来高・売買代金が増加している
#   5. 株価が切り返し始めている
#   6. 逆張りBUY指標が点灯している
#   7. ただし順張りSELL指標が強すぎる場合は見送り
#
# SELL逆張りを許可する条件:
#   1. 長い間、株価が75MAの上で推移している、または現在75MA上
#   2. 25MAと75MAが収束してきている、または現在ギャップが小さい
#   3. MA25/MA75から上方乖離している
#   4. 出来高・売買代金が増加している
#   5. 株価が反落し始めている
#   6. 逆張りSELL指標が点灯している
#   7. ただし順張りBUY指標が強すぎる場合は見送り
#
# 使う主な列:
#   close/close_price, open/open_price, high/high_price, low/low_price
#   volume, turnover
#   volume_ma20, turnover_ma20
#   ma25/ma75, daily_ma25/daily_ma75
#   below_ma75_count, below_ma75_ratio, above_ma75_count, above_ma75_ratio
#   ma25_ma75_gap_pct, ma25_ma75_gap_pct_prev, ma25_ma75_gap_pct_ago
#   price_change_3, price_change_5, price_change_20
#   score_buy, score_sell, slope, score_slope, slope_atr_scaled
#   contrarian_buy_score, contrarian_sell_score
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


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
    if s in {"BUY", "LONG", "2", "買", "買い"}:
        return "BUY"
    if s in {"SELL", "SHORT", "1", "売", "売り"}:
        return "SELL"
    return s


def _norm_symbol(v: Any) -> str:
    s = str(v or "").strip()
    if s.endswith(".0") and s[:-2].isdigit():
        return s[:-2]
    return s


def _as_pct(v: float) -> float:
    if abs(v) <= 1.0:
        return v * 100.0
    return v


def _get_ohlcv(row: dict) -> dict:
    open_ = _safe_float(_first(row, ("open", "open_price"), 0.0), 0.0)
    high = _safe_float(_first(row, ("high", "high_price"), 0.0), 0.0)
    low = _safe_float(_first(row, ("low", "low_price"), 0.0), 0.0)
    close = _safe_float(_first(row, ("close", "close_price", "price", "current_price"), 0.0), 0.0)
    volume = _safe_float(_first(row, ("volume", "Volume", "出来高"), 0.0), 0.0)
    turnover = _safe_float(_first(row, ("turnover", "trading_value", "売買代金"), 0.0), 0.0)
    if turnover <= 0 and close > 0 and volume > 0:
        turnover = close * volume
    return {"open": open_, "high": high, "low": low, "close": close, "volume": volume, "turnover": turnover}


def _get_ma(row: dict) -> tuple[float, float]:
    ma25 = _safe_float(_first(row, ("ma25", "MA25", "ma_25", "daily_ma25", "MA_25"), 0.0), 0.0)
    ma75 = _safe_float(_first(row, ("ma75", "MA75", "ma_75", "daily_ma75", "MA_75"), 0.0), 0.0)
    return ma25, ma75


def _dev_pct(close: float, ma: float) -> float:
    try:
        if close <= 0 or ma <= 0:
            return 0.0
        return ((close - ma) / ma) * 100.0
    except Exception:
        return 0.0


def _gap_pct(ma25: float, ma75: float) -> float:
    try:
        if ma25 <= 0 or ma75 <= 0:
            return 0.0
        return abs(ma25 - ma75) / ma75 * 100.0
    except Exception:
        return 0.0


def _bar_metrics(open_: float, high: float, low: float, close: float) -> dict:
    rng = max(high - low, 0.0)
    if high <= 0 or low <= 0 or close <= 0 or rng <= 0:
        return {"close_pos": 0.5, "range_pct": 0.0, "lower_wick_ratio": 0.0, "upper_wick_ratio": 0.0}
    lower_body = min(open_ if open_ > 0 else close, close)
    upper_body = max(open_ if open_ > 0 else close, close)
    return {
        "close_pos": (close - low) / rng,
        "range_pct": (rng / close) * 100.0,
        "lower_wick_ratio": max(lower_body - low, 0.0) / rng,
        "upper_wick_ratio": max(high - upper_body, 0.0) / rng,
    }


def _volume_metrics(row: dict, volume: float, turnover: float) -> dict:
    volume_ma = _safe_float(_first(row, ("volume_ma20", "vol_ma20", "avg_volume20", "volume_avg20"), 0.0), 0.0)
    turnover_ma = _safe_float(_first(row, ("turnover_ma20", "trading_value_ma20", "avg_turnover20", "turnover_avg20"), 0.0), 0.0)
    volume_ratio = (volume / volume_ma) if volume_ma > 0 else 1.0
    turnover_ratio = (turnover / turnover_ma) if turnover_ma > 0 else 1.0
    return {"volume_ma20": volume_ma, "turnover_ma20": turnover_ma, "volume_ratio": volume_ratio, "turnover_ratio": turnover_ratio}


def _trend_metrics(row: dict) -> dict:
    pc3 = _as_pct(_safe_float(_first(row, ("price_change_3", "change_3", "ret_3", "return_3"), 0.0), 0.0))
    pc5 = _as_pct(_safe_float(_first(row, ("price_change_5", "change_5", "ret_5", "return_5"), 0.0), 0.0))
    pc20 = _as_pct(_safe_float(_first(row, ("price_change_20", "change_20", "ret_20", "return_20"), 0.0), 0.0))
    return {"price_change_3_pct": pc3, "price_change_5_pct": pc5, "price_change_20_pct": pc20}


def _ma75_stay(row: dict, close: float, ma75: float, side: str) -> dict:
    lookback = _env_float("ENTRY_CLIMAX_MA75_STAY_LOOKBACK", 20.0)
    min_ratio = _env_float("ENTRY_CLIMAX_MA75_STAY_MIN_RATIO", 0.70)
    min_count = _env_float("ENTRY_CLIMAX_MA75_STAY_MIN_COUNT", 12.0)

    below_count = _safe_float(_first(row, ("below_ma75_count", "close_below_ma75_count", "below_75ma_count"), 0.0), 0.0)
    below_ratio = _safe_float(_first(row, ("below_ma75_ratio", "close_below_ma75_ratio", "below_75ma_ratio"), 0.0), 0.0)
    above_count = _safe_float(_first(row, ("above_ma75_count", "close_above_ma75_count", "above_75ma_count"), 0.0), 0.0)
    above_ratio = _safe_float(_first(row, ("above_ma75_ratio", "close_above_ma75_ratio", "above_75ma_ratio"), 0.0), 0.0)

    if below_ratio > 1.0:
        below_ratio /= 100.0
    if above_ratio > 1.0:
        above_ratio /= 100.0
    if below_ratio <= 0 and below_count > 0 and lookback > 0:
        below_ratio = below_count / lookback
    if above_ratio <= 0 and above_count > 0 and lookback > 0:
        above_ratio = above_count / lookback

    if side == "BUY":
        current_ok = bool(close > 0 and ma75 > 0 and close < ma75)
        strong_ok = current_ok and (below_ratio >= min_ratio or below_count >= min_count)
        weak_ok = current_ok and below_count <= 0 and below_ratio <= 0
        return {"ok": strong_ok or weak_ok, "strong_ok": strong_ok, "weak_ok": weak_ok, "count": below_count, "ratio": below_ratio}

    current_ok = bool(close > 0 and ma75 > 0 and close > ma75)
    strong_ok = current_ok and (above_ratio >= min_ratio or above_count >= min_count)
    weak_ok = current_ok and above_count <= 0 and above_ratio <= 0
    return {"ok": strong_ok or weak_ok, "strong_ok": strong_ok, "weak_ok": weak_ok, "count": above_count, "ratio": above_ratio}


def _ma25_ma75_convergence(row: dict, ma25: float, ma75: float) -> dict:
    gap_now = _safe_float(_first(row, ("ma25_ma75_gap_pct", "ma_25_75_gap_pct", "ma2575_gap_pct"), 0.0), 0.0)
    if gap_now <= 0:
        gap_now = _gap_pct(ma25, ma75)
    gap_prev = _safe_float(_first(row, ("ma25_ma75_gap_pct_prev", "ma25_ma75_gap_prev", "ma_25_75_gap_pct_prev"), 0.0), 0.0)
    gap_ago = _safe_float(_first(row, ("ma25_ma75_gap_pct_ago", "ma25_ma75_gap_ago", "ma25_ma75_gap_pct_20ago"), 0.0), 0.0)
    max_gap = _env_float("ENTRY_CLIMAX_MA25_MA75_MAX_GAP_PCT", 1.2)
    min_shrink = _env_float("ENTRY_CLIMAX_MA25_MA75_MIN_SHRINK_PCT", 0.20)
    small_gap = bool(gap_now > 0 and gap_now <= max_gap)
    shrinking = bool((gap_prev > 0 and gap_now <= gap_prev * (1.0 - min_shrink)) or (gap_ago > 0 and gap_now <= gap_ago * (1.0 - min_shrink)))
    weak_ok = small_gap and gap_prev <= 0 and gap_ago <= 0
    strong_ok = small_gap and shrinking
    return {"ok": strong_ok or weak_ok, "strong_ok": strong_ok, "weak_ok": weak_ok, "gap_now": gap_now, "gap_prev": gap_prev, "gap_ago": gap_ago, "small_gap": small_gap, "shrinking": shrinking}


def _score_values(row: dict) -> dict:
    score_buy = _safe_float(_first(row, ("score_buy", "buy_score", "disp_buy_score", "buy"), 0.0), 0.0)
    score_sell = _safe_float(_first(row, ("score_sell", "sell_score", "disp_sell_score", "sell"), 0.0), 0.0)
    trend_strength = _safe_float(_first(row, ("trend_strength", "direction_strength"), 0.0), 0.0)
    slope = _safe_float(_first(row, ("slope_atr_scaled", "score_slope", "disp_slope", "slope"), 0.0), 0.0)
    contrarian_buy = _safe_float(_first(row, ("contrarian_buy_score", "reverse_buy_score", "reversal_buy_score"), 0.0), 0.0)
    contrarian_sell = _safe_float(_first(row, ("contrarian_sell_score", "reverse_sell_score", "reversal_sell_score"), 0.0), 0.0)
    return {"score_buy": score_buy, "score_sell": score_sell, "trend_strength": trend_strength, "slope": slope, "contrarian_buy": contrarian_buy, "contrarian_sell": contrarian_sell}


def _contrarian_signal_ok(row: dict, side: str, scores: dict, bar: dict, trend: dict, dev25: float, dev75: float) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if side == "BUY":
        if scores["contrarian_buy"] >= _env_float("ENTRY_CONTRARIAN_BUY_MIN_SCORE", 1.0):
            reasons.append("contrarian_buy_score")
        if bar["close_pos"] >= _env_float("ENTRY_CONTRARIAN_BUY_MIN_CLOSE_POS", 0.55):
            reasons.append("close_recovered")
        if trend["price_change_3_pct"] >= _env_float("ENTRY_CONTRARIAN_BUY_MIN_3BAR_CHANGE_PCT", 0.0):
            reasons.append("short_term_turn_up")
        if dev25 <= _env_float("ENTRY_CONTRARIAN_BUY_MA25_DEV_PCT", -0.5) or dev75 <= _env_float("ENTRY_CONTRARIAN_BUY_MA75_DEV_PCT", -0.5):
            reasons.append("negative_ma_deviation")
        return len(reasons) >= int(_env_float("ENTRY_CONTRARIAN_SIGNAL_MIN_HITS", 2.0)), reasons

    if scores["contrarian_sell"] >= _env_float("ENTRY_CONTRARIAN_SELL_MIN_SCORE", 1.0):
        reasons.append("contrarian_sell_score")
    if bar["close_pos"] <= _env_float("ENTRY_CONTRARIAN_SELL_MAX_CLOSE_POS", 0.45):
        reasons.append("close_faded")
    if trend["price_change_3_pct"] <= _env_float("ENTRY_CONTRARIAN_SELL_MAX_3BAR_CHANGE_PCT", 0.0):
        reasons.append("short_term_turn_down")
    if dev25 >= _env_float("ENTRY_CONTRARIAN_SELL_MA25_DEV_PCT", 0.5) or dev75 >= _env_float("ENTRY_CONTRARIAN_SELL_MA75_DEV_PCT", 0.5):
        reasons.append("positive_ma_deviation")
    return len(reasons) >= int(_env_float("ENTRY_CONTRARIAN_SIGNAL_MIN_HITS", 2.0)), reasons


def _trend_follow_too_strong(side: str, scores: dict, trend: dict) -> tuple[bool, str]:
    max_opposite = _env_float("ENTRY_CONTRARIAN_MAX_OPPOSITE_TREND_SCORE", 6.0)
    max_slope = _env_float("ENTRY_CONTRARIAN_MAX_OPPOSITE_SLOPE", 0.08)
    max_5bar = _env_float("ENTRY_CONTRARIAN_MAX_OPPOSITE_5BAR_PCT", 1.2)

    if side == "BUY":
        if scores["score_sell"] >= max_opposite:
            return True, "sell_score_too_strong"
        if scores["slope"] <= -max_slope:
            return True, "down_slope_too_strong"
        if trend["price_change_5_pct"] <= -max_5bar:
            return True, "down_momentum_too_strong"
        return False, ""

    if scores["score_buy"] >= max_opposite:
        return True, "buy_score_too_strong"
    if scores["slope"] >= max_slope:
        return True, "up_slope_too_strong"
    if trend["price_change_5_pct"] >= max_5bar:
        return True, "up_momentum_too_strong"
    return False, ""


def detect_climax_reversal(entry_row: Any, side: Any = None) -> dict:
    if not _env_bool("ENTRY_CLIMAX_REVERSAL_ENABLED", True):
        return {"allow_exception": False, "climax_type": "", "climax_score": 0.0, "reason": "disabled", "details": {}}

    try:
        row = entry_row if isinstance(entry_row, dict) else dict(entry_row.to_dict()) if hasattr(entry_row, "to_dict") else {}
    except Exception:
        row = {}

    side_n = _norm_side(side or _first(row, ("side", "entry_decision", "ai_side"), ""))
    symbol = _norm_symbol(_first(row, ("symbol", "code", "stock_code"), ""))

    ohlcv = _get_ohlcv(row)
    open_, high, low, close = ohlcv["open"], ohlcv["high"], ohlcv["low"], ohlcv["close"]
    volume, turnover = ohlcv["volume"], ohlcv["turnover"]
    if side_n not in {"BUY", "SELL"} or close <= 0 or high <= 0 or low <= 0 or volume <= 0:
        return {"allow_exception": False, "climax_type": "", "climax_score": 0.0, "reason": "missing_or_invalid_input", "details": ohlcv}

    ma25, ma75 = _get_ma(row)
    dev25 = _dev_pct(close, ma25)
    dev75 = _dev_pct(close, ma75)
    bar = _bar_metrics(open_, high, low, close)
    volm = _volume_metrics(row, volume, turnover)
    trend = _trend_metrics(row)
    stay = _ma75_stay(row, close, ma75, side_n)
    converge = _ma25_ma75_convergence(row, ma25, ma75)
    scores = _score_values(row)

    min_volume = _env_float("ENTRY_CLIMAX_MIN_VOLUME", 30000.0)
    min_turnover = _env_float("ENTRY_CLIMAX_MIN_TURNOVER", 10000000.0)
    min_volume_ratio = _env_float("ENTRY_CLIMAX_MIN_VOLUME_RATIO", 1.5)
    min_turnover_ratio = _env_float("ENTRY_CLIMAX_MIN_TURNOVER_RATIO", 1.3)

    if volume < min_volume or turnover < min_turnover:
        return {"allow_exception": False, "climax_type": "", "climax_score": 0.0, "reason": "liquidity_not_enough", "details": {**ohlcv, "min_volume": min_volume, "min_turnover": min_turnover}}

    volume_ok = bool(volm["volume_ratio"] >= min_volume_ratio or volm["volume_ma20"] <= 0)
    turnover_ok = bool(volm["turnover_ratio"] >= min_turnover_ratio or volm["turnover_ma20"] <= 0)

    if not volume_ok or not turnover_ok:
        return {"allow_exception": False, "climax_type": "", "climax_score": 0.0, "reason": "volume_turnover_not_increasing", "details": {**ohlcv, **volm}}

    if not stay["ok"]:
        return {"allow_exception": False, "climax_type": "", "climax_score": 0.0, "reason": "ma75_stay_not_enough", "details": {**ohlcv, **stay}}

    if _env_bool("ENTRY_CLIMAX_REQUIRE_MA25_MA75_CONVERGENCE", True) and not converge["ok"]:
        return {"allow_exception": False, "climax_type": "", "climax_score": 0.0, "reason": "ma25_ma75_not_converging", "details": {**ohlcv, **converge}}

    signal_ok, signal_reasons = _contrarian_signal_ok(row, side_n, scores, bar, trend, dev25, dev75)
    if not signal_ok:
        return {"allow_exception": False, "climax_type": "", "climax_score": 0.0, "reason": "contrarian_signal_not_enough", "details": {**ohlcv, **bar, **trend, "dev25": dev25, "dev75": dev75, **scores}}

    trend_ng, trend_reason = _trend_follow_too_strong(side_n, scores, trend)
    if trend_ng:
        logger.warning(
            "[ENTRY CLIMAX REVERSAL] REVIEW symbol=%s side=%s reason=opposite_trend_too_strong detail=%s score_buy=%.3f score_sell=%.3f slope=%.6f pc5=%.3f",
            symbol, side_n, trend_reason, scores["score_buy"], scores["score_sell"], scores["slope"], trend["price_change_5_pct"],
        )
        return {"allow_exception": False, "climax_type": "", "climax_score": 0.0, "reason": trend_reason, "details": {**ohlcv, **trend, **scores}}

    base_score = 0.0
    reasons: list[str] = []

    if stay["ok"]:
        base_score += 1.5; reasons.append("ma75_long_stay")
    if converge["ok"]:
        base_score += 1.5; reasons.append("ma25_ma75_converging")
    if volume_ok:
        base_score += 1.0; reasons.append("volume_increasing")
    if turnover_ok:
        base_score += 1.0; reasons.append("turnover_increasing")
    base_score += min(1.5, len(signal_reasons) * 0.5)
    reasons.extend(signal_reasons)

    if side_n == "BUY":
        if trend["price_change_20_pct"] <= _env_float("ENTRY_ABSORPTION_MIN_20BAR_DROP_PCT", -1.0):
            base_score += 0.5; reasons.append("prior_downtrend")
        if trend["price_change_3_pct"] >= 0 or bar["close_pos"] >= 0.55:
            base_score += 0.5; reasons.append("turning_up")
        climax_type = "contrarian_buy_ma_deviation_volume_reversal"
        min_score = _env_float("ENTRY_CONTRARIAN_BUY_MIN_TOTAL_SCORE", 5.0)
    else:
        if trend["price_change_20_pct"] >= _env_float("ENTRY_EXHAUSTION_MIN_20BAR_RISE_PCT", 1.0):
            base_score += 0.5; reasons.append("prior_uptrend")
        if trend["price_change_3_pct"] <= 0 or bar["close_pos"] <= 0.45:
            base_score += 0.5; reasons.append("turning_down")
        climax_type = "contrarian_sell_ma_deviation_volume_reversal"
        min_score = _env_float("ENTRY_CONTRARIAN_SELL_MIN_TOTAL_SCORE", 5.0)

    allow = base_score >= min_score
    if not allow:
        climax_type = ""

    logger.warning(
        "[ENTRY CLIMAX REVERSAL] %s symbol=%s side=%s type=%s score=%.2f min=%.2f reasons=%s close=%.4f ma25=%.4f ma75=%.4f dev25=%.3f dev75=%.3f stay_ratio=%.3f stay_count=%.1f gap_now=%.3f gap_prev=%.3f gap_ago=%.3f volume=%.0f turnover=%.0f vol_ratio=%.2f turnover_ratio=%.2f pc3=%.3f pc5=%.3f pc20=%.3f score_buy=%.3f score_sell=%.3f slope=%.6f",
        "OK" if allow else "NG",
        symbol, side_n, climax_type, base_score, min_score, reasons,
        close, ma25, ma75, dev25, dev75, stay["ratio"], stay["count"], converge["gap_now"], converge["gap_prev"], converge["gap_ago"],
        volume, turnover, volm["volume_ratio"], volm["turnover_ratio"], trend["price_change_3_pct"], trend["price_change_5_pct"], trend["price_change_20_pct"],
        scores["score_buy"], scores["score_sell"], scores["slope"],
    )

    return {
        "allow_exception": allow,
        "climax_type": climax_type,
        "climax_score": float(base_score),
        "reason": "|".join(reasons),
        "details": {
            **ohlcv,
            **bar,
            **volm,
            **trend,
            **stay,
            **converge,
            **scores,
            "dev25": dev25,
            "dev75": dev75,
            "trend_follow_review_reason": trend_reason,
        },
    }


__all__ = ["detect_climax_reversal"]
