# ============================================================
# File   : trading/entry/ma_cross_state_detector.py
# Version: Ver01-MA-CROSS-CONTINUATION-STATE
# ------------------------------------------------------------
# ゴールデンクロス/デッドクロスの一瞬サインではなく、
# クロス後のMA位置関係・継続本数・乖離拡大/縮小を判定する。
#
# 目的:
#   - golden_cross_event だけでなく、その後の
#     ma5 > ma25 の継続状態をBUY方向として評価する
#   - dead_cross_event だけでなく、その後の
#     ma5 < ma25 の継続状態をSELL方向として評価する
#   - 乖離が広がりすぎ/縮小し始めた場合は伸び切り警戒にする
#
# 利用列候補:
#   close, close_price
#   ma5, ma25, ma75, daily_ma5, daily_ma25, daily_ma75
#   ma5_above_ma25_bars, ma5_below_ma25_bars
#   ma5_ma25_gap_pct, ma5_ma25_gap_pct_prev, ma5_ma25_gap_pct_ago
#   ma25_ma75_gap_pct, ma25_ma75_gap_pct_prev, ma25_ma75_gap_pct_ago
#   ma25_slope, ma75_slope, daily_ma25_slope, daily_ma75_slope
# ============================================================

from __future__ import annotations

import logging
import math
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
        x = float(v)
        if math.isnan(x) or math.isinf(x):
            return float(default)
        return x
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


def _pct_gap(a: float, b: float) -> float:
    try:
        if a <= 0 or b <= 0:
            return 0.0
        return abs(a - b) / b * 100.0
    except Exception:
        return 0.0


def _get_ma(row: dict) -> tuple[float, float, float, float]:
    close = _safe_float(_first(row, ("close", "close_price", "price", "current_price", "daily_close"), 0.0), 0.0)
    ma5 = _safe_float(_first(row, ("ma5", "MA5", "ma_5", "daily_ma5", "MA_5"), 0.0), 0.0)
    ma25 = _safe_float(_first(row, ("ma25", "MA25", "ma_25", "daily_ma25", "MA_25"), 0.0), 0.0)
    ma75 = _safe_float(_first(row, ("ma75", "MA75", "ma_75", "daily_ma75", "MA_75"), 0.0), 0.0)
    return close, ma5, ma25, ma75


def _gap_metrics(row: dict, a_name: str, a: float, b_name: str, b: float) -> dict:
    base = f"{a_name}_{b_name}_gap_pct"
    gap_now = _safe_float(_first(row, (base, f"{a_name}{b_name}_gap_pct", f"{a_name}_{b_name}_gap"), 0.0), 0.0)
    if gap_now <= 0:
        gap_now = _pct_gap(a, b)
    gap_prev = _safe_float(_first(row, (f"{base}_prev", f"{a_name}_{b_name}_gap_prev"), 0.0), 0.0)
    gap_ago = _safe_float(_first(row, (f"{base}_ago", f"{a_name}_{b_name}_gap_ago", f"{base}_20ago"), 0.0), 0.0)
    min_expand = _env_float("ENTRY_MA_CROSS_MIN_GAP_EXPAND_PCT", 0.10)
    min_shrink = _env_float("ENTRY_MA_CROSS_MIN_GAP_SHRINK_PCT", 0.10)
    widening = bool((gap_prev > 0 and gap_now >= gap_prev * (1.0 + min_expand)) or (gap_ago > 0 and gap_now >= gap_ago * (1.0 + min_expand)))
    shrinking = bool((gap_prev > 0 and gap_now <= gap_prev * (1.0 - min_shrink)) or (gap_ago > 0 and gap_now <= gap_ago * (1.0 - min_shrink)))
    return {"gap_now": gap_now, "gap_prev": gap_prev, "gap_ago": gap_ago, "widening": widening, "shrinking": shrinking}


def _slope(row: dict, ma_key: str) -> float:
    n = ma_key.replace("ma", "")
    return _safe_float(_first(row, (
        f"{ma_key}_slope", f"{ma_key}_Slope", f"MA_{n}_Slope", f"MA_{n}_slope",
        f"daily_{ma_key}_slope", f"daily_ma{n}_slope", f"daily_MA_{n}_Slope",
    ), 0.0), 0.0)


def detect_ma_cross_state(row: Any) -> dict:
    try:
        d = row if isinstance(row, dict) else dict(row.to_dict()) if hasattr(row, "to_dict") else {}
    except Exception:
        d = {}

    if not _env_bool("ENTRY_MA_CROSS_STATE_ENABLED", True):
        return {"enabled": False, "score_delta": 0.0, "state": "disabled", "reasons": [], "details": {}}

    close, ma5, ma25, ma75 = _get_ma(d)
    if close <= 0 or ma5 <= 0 or ma25 <= 0 or ma75 <= 0:
        return {"enabled": True, "score_delta": 0.0, "state": "ma_missing", "reasons": [], "details": {"close": close, "ma5": ma5, "ma25": ma25, "ma75": ma75}}

    above_bars = _safe_float(_first(d, ("ma5_above_ma25_bars", "ma5_gt_ma25_bars", "golden_cross_age", "gc_age"), 0.0), 0.0)
    below_bars = _safe_float(_first(d, ("ma5_below_ma25_bars", "ma5_lt_ma25_bars", "dead_cross_age", "dc_age"), 0.0), 0.0)
    min_bars = _env_float("ENTRY_MA_CROSS_CONTINUATION_MIN_BARS", 3.0)
    max_mature_bars = _env_float("ENTRY_MA_CROSS_MATURE_BARS", 30.0)
    max_gap = _env_float("ENTRY_MA_CROSS_MAX_GAP_PCT", 2.5)

    gap_5_25 = _gap_metrics(d, "ma5", ma5, "ma25", ma25)
    gap_25_75 = _gap_metrics(d, "ma25", ma25, "ma75", ma75)
    ma25_slope = _slope(d, "ma25")
    ma75_slope = _slope(d, "ma75")

    reasons: list[str] = []
    score_delta = 0.0
    state = "neutral"

    bullish_stack = bool(close > ma5 > ma25 > ma75)
    bearish_stack = bool(close < ma5 < ma25 < ma75)
    ma5_above = bool(ma5 > ma25)
    ma5_below = bool(ma5 < ma25)

    # --------------------------------------------------------
    # BUY方向: ゴールデンクロス後の継続状態
    # --------------------------------------------------------
    if ma5_above:
        if above_bars >= min_bars:
            score_delta += 0.8
            reasons.append("ma5_above_ma25_continuation")
            state = "golden_cross_continuation"
        elif above_bars <= 0:
            score_delta += 0.4
            reasons.append("ma5_above_ma25_current")
            state = "golden_cross_current"
        else:
            score_delta += 0.5
            reasons.append("golden_cross_recent")
            state = "golden_cross_recent"

        if bullish_stack:
            score_delta += 0.9
            reasons.append("bullish_ma_stack")
            state = "bullish_stack_continuation"
        elif close > ma5 and ma25 >= ma75:
            score_delta += 0.5
            reasons.append("price_above_ma5_ma25_above_ma75")

        if gap_5_25["widening"]:
            score_delta += 0.4
            reasons.append("ma5_ma25_gap_widening")
        if ma25_slope > 0:
            score_delta += 0.3
            reasons.append("ma25_slope_up")
        if ma75_slope > 0:
            score_delta += 0.2
            reasons.append("ma75_slope_up")

        if gap_5_25["shrinking"]:
            score_delta -= 0.6
            reasons.append("ma5_ma25_gap_shrinking_caution")
            state = "golden_cross_exhaustion" if state.startswith("golden") or "bullish" in state else state
        if gap_5_25["gap_now"] >= max_gap:
            score_delta -= 0.5
            reasons.append("ma5_ma25_gap_too_wide_caution")
            state = "golden_cross_mature"
        if above_bars >= max_mature_bars:
            score_delta -= 0.3
            reasons.append("golden_cross_age_mature")
            state = "golden_cross_mature"

    # --------------------------------------------------------
    # SELL方向: デッドクロス後の継続状態
    # --------------------------------------------------------
    if ma5_below:
        if below_bars >= min_bars:
            score_delta -= 0.8
            reasons.append("ma5_below_ma25_continuation")
            state = "dead_cross_continuation"
        elif below_bars <= 0:
            score_delta -= 0.4
            reasons.append("ma5_below_ma25_current")
            state = "dead_cross_current"
        else:
            score_delta -= 0.5
            reasons.append("dead_cross_recent")
            state = "dead_cross_recent"

        if bearish_stack:
            score_delta -= 0.9
            reasons.append("bearish_ma_stack")
            state = "bearish_stack_continuation"
        elif close < ma5 and ma25 <= ma75:
            score_delta -= 0.5
            reasons.append("price_below_ma5_ma25_below_ma75")

        if gap_5_25["widening"]:
            score_delta -= 0.4
            reasons.append("ma5_ma25_gap_widening")
        if ma25_slope < 0:
            score_delta -= 0.3
            reasons.append("ma25_slope_down")
        if ma75_slope < 0:
            score_delta -= 0.2
            reasons.append("ma75_slope_down")

        if gap_5_25["shrinking"]:
            score_delta += 0.6
            reasons.append("ma5_ma25_gap_shrinking_caution")
            state = "dead_cross_exhaustion" if state.startswith("dead") or "bearish" in state else state
        if gap_5_25["gap_now"] >= max_gap:
            score_delta += 0.5
            reasons.append("ma5_ma25_gap_too_wide_caution")
            state = "dead_cross_mature"
        if below_bars >= max_mature_bars:
            score_delta += 0.3
            reasons.append("dead_cross_age_mature")
            state = "dead_cross_mature"

    # 安全上限。既存スコアを壊しすぎない。
    cap = abs(_env_float("ENTRY_MA_CROSS_STATE_MAX_SCORE_DELTA", 2.0))
    score_delta = max(-cap, min(cap, score_delta))

    details = {
        "close": close,
        "ma5": ma5,
        "ma25": ma25,
        "ma75": ma75,
        "above_bars": above_bars,
        "below_bars": below_bars,
        "gap_5_25": gap_5_25,
        "gap_25_75": gap_25_75,
        "ma25_slope": ma25_slope,
        "ma75_slope": ma75_slope,
        "bullish_stack": bullish_stack,
        "bearish_stack": bearish_stack,
    }

    logger.info(
        "[MA CROSS STATE] state=%s score_delta=%.3f reasons=%s close=%.4f ma5=%.4f ma25=%.4f ma75=%.4f above_bars=%.1f below_bars=%.1f gap5_25=%.3f widening=%s shrinking=%s",
        state,
        score_delta,
        reasons,
        close,
        ma5,
        ma25,
        ma75,
        above_bars,
        below_bars,
        gap_5_25["gap_now"],
        gap_5_25["widening"],
        gap_5_25["shrinking"],
    )

    return {"enabled": True, "score_delta": float(score_delta), "state": state, "reasons": reasons, "details": details}


__all__ = ["detect_ma_cross_state"]
