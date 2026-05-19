# ============================================================
# File   : trading/entry/vwap_state_detector.py
# Version: Ver01-VWAP-CONTINUATION-STATE
# ------------------------------------------------------------
# VWAP上抜け/下抜けの一瞬サインではなく、
# その後の VWAP 上/下の継続状態・乖離拡大/縮小を判定する。
#
# BUY評価:
#   - close > vwap が継続
#   - vwap_gap_pct が拡大中
#   - ただし乖離が大きすぎる/縮小中は伸び切り警戒
#
# SELL評価:
#   - close < vwap が継続
#   - vwap_gap_pct が拡大中
#   - ただし乖離が大きすぎる/縮小中は下げ切り警戒
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


def detect_vwap_state(row: Any) -> dict:
    d = _row_to_dict(row)

    if not _env_bool("ENTRY_VWAP_STATE_ENABLED", True):
        return {"enabled": False, "score_delta": 0.0, "state": "disabled", "reasons": [], "details": {}}

    close = _safe_float(_first(d, ("close", "close_price", "price", "current_price"), 0.0), 0.0)
    vwap = _safe_float(_first(d, ("vwap", "VWAP", "vwap_price", "session_vwap"), 0.0), 0.0)

    if close <= 0 or vwap <= 0:
        return {
            "enabled": True,
            "score_delta": 0.0,
            "state": "vwap_missing",
            "reasons": [],
            "details": {"close": close, "vwap": vwap},
        }

    above_bars = _safe_float(_first(d, ("price_above_vwap_bars", "above_vwap_bars", "vwap_above_bars"), 0.0), 0.0)
    below_bars = _safe_float(_first(d, ("price_below_vwap_bars", "below_vwap_bars", "vwap_below_bars"), 0.0), 0.0)

    gap_pct = _safe_float(_first(d, ("vwap_gap_pct", "price_vwap_gap_pct"), 0.0), 0.0)
    if gap_pct == 0.0:
        gap_pct = abs(close - vwap) / vwap * 100.0

    gap_prev = _safe_float(_first(d, ("vwap_gap_pct_prev", "price_vwap_gap_pct_prev"), 0.0), 0.0)
    gap_ago = _safe_float(_first(d, ("vwap_gap_pct_ago", "price_vwap_gap_pct_ago"), 0.0), 0.0)

    min_bars = _env_float("ENTRY_VWAP_CONTINUATION_MIN_BARS", 3.0)
    mature_bars = _env_float("ENTRY_VWAP_MATURE_BARS", 20.0)
    max_gap = _env_float("ENTRY_VWAP_MAX_GAP_PCT", 1.20)
    min_expand = _env_float("ENTRY_VWAP_MIN_GAP_EXPAND_PCT", 0.08)
    min_shrink = _env_float("ENTRY_VWAP_MIN_GAP_SHRINK_PCT", 0.08)

    widening = bool((gap_prev > 0 and gap_pct >= gap_prev * (1.0 + min_expand)) or (gap_ago > 0 and gap_pct >= gap_ago * (1.0 + min_expand)))
    shrinking = bool((gap_prev > 0 and gap_pct <= gap_prev * (1.0 - min_shrink)) or (gap_ago > 0 and gap_pct <= gap_ago * (1.0 - min_shrink)))

    reasons: list[str] = []
    score_delta = 0.0
    state = "neutral"

    above = close > vwap
    below = close < vwap

    if above:
        if above_bars >= min_bars:
            score_delta += 0.8
            reasons.append("price_above_vwap_continuation")
            state = "above_vwap_continuation"
        elif above_bars > 0:
            score_delta += 0.5
            reasons.append("price_above_vwap_recent")
            state = "above_vwap_recent"
        else:
            score_delta += 0.4
            reasons.append("price_above_vwap_current")
            state = "above_vwap_current"

        if widening:
            score_delta += 0.4
            reasons.append("vwap_gap_widening")
        if shrinking:
            score_delta -= 0.5
            reasons.append("vwap_gap_shrinking_caution")
            state = "above_vwap_exhaustion"
        if gap_pct >= max_gap or above_bars >= mature_bars:
            score_delta -= 0.5
            reasons.append("vwap_gap_or_age_mature")
            state = "above_vwap_mature"

    elif below:
        if below_bars >= min_bars:
            score_delta -= 0.8
            reasons.append("price_below_vwap_continuation")
            state = "below_vwap_continuation"
        elif below_bars > 0:
            score_delta -= 0.5
            reasons.append("price_below_vwap_recent")
            state = "below_vwap_recent"
        else:
            score_delta -= 0.4
            reasons.append("price_below_vwap_current")
            state = "below_vwap_current"

        if widening:
            score_delta -= 0.4
            reasons.append("vwap_gap_widening")
        if shrinking:
            score_delta += 0.5
            reasons.append("vwap_gap_shrinking_caution")
            state = "below_vwap_exhaustion"
        if gap_pct >= max_gap or below_bars >= mature_bars:
            score_delta += 0.5
            reasons.append("vwap_gap_or_age_mature")
            state = "below_vwap_mature"

    cap = abs(_env_float("ENTRY_VWAP_STATE_MAX_SCORE_DELTA", 1.5))
    score_delta = max(-cap, min(cap, score_delta))

    details = {
        "close": close,
        "vwap": vwap,
        "above_bars": above_bars,
        "below_bars": below_bars,
        "gap_pct": gap_pct,
        "gap_prev": gap_prev,
        "gap_ago": gap_ago,
        "widening": widening,
        "shrinking": shrinking,
        "above": above,
        "below": below,
    }

    logger.info(
        "[VWAP STATE] state=%s score_delta=%.3f reasons=%s close=%.4f vwap=%.4f above_bars=%.1f below_bars=%.1f gap=%.3f widening=%s shrinking=%s",
        state,
        score_delta,
        reasons,
        close,
        vwap,
        above_bars,
        below_bars,
        gap_pct,
        widening,
        shrinking,
    )

    return {"enabled": True, "score_delta": float(score_delta), "state": state, "reasons": reasons, "details": details}


__all__ = ["detect_vwap_state"]
