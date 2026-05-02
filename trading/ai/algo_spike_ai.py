# ============================================================
# File   : trading/ai/algo_spike_ai.py
# Version: ALGO-SPIKE-DETECTION-AI-PRODUCTION-STABLE
# ------------------------------------------------------------
# ✔ アルゴスパイク検出AI
# ✔ HFT / 板掃除検出
# ✔ tick burst
# ✔ volume spike
# ✔ VWAP deviation
# ✔ micro momentum
# ✔ spread compression
# ✔ volume slope
# ✔ NaN / None / inf 安全
# ✔ DataFrame API
# ✔ production safe
# ============================================================

from __future__ import annotations

import logging
import math
import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# safe float
# ============================================================

def _safe(v):

    try:

        f = float(v)

        if not math.isfinite(f):
            return None

        return f

    except Exception:

        return None


# ============================================================
# price spike
# ============================================================

def _price_spike(row):

    change = _safe(row.get("price_change"))
    atr = _safe(row.get("atr"))

    if change is None or atr is None or atr == 0:
        return 0

    ratio = abs(change) / atr

    if ratio > 3:
        return 3

    if ratio > 2:
        return 2

    if ratio > 1:
        return 1

    return 0


# ============================================================
# volume spike
# ============================================================

def _volume_spike(row):

    vol = _safe(row.get("volume"))
    vol_avg = _safe(row.get("volume_avg"))

    if vol is None or vol_avg is None or vol_avg == 0:
        return 0

    ratio = vol / vol_avg

    if ratio > 5:
        return 3

    if ratio > 3:
        return 2

    if ratio > 2:
        return 1

    return 0


# ============================================================
# tick burst
# ============================================================

def _tick_burst(row):

    ticks = _safe(row.get("tick_count"))
    ticks_avg = _safe(row.get("tick_avg"))

    if ticks is None or ticks_avg is None or ticks_avg == 0:
        return 0

    ratio = ticks / ticks_avg

    if ratio > 4:
        return 3

    if ratio > 2:
        return 2

    if ratio > 1.5:
        return 1

    return 0


# ============================================================
# VWAP deviation
# ============================================================

def _vwap_deviation(row):

    price = _safe(row.get("close_price"))
    vwap = _safe(row.get("vwap"))

    if price is None or vwap is None or vwap == 0:
        return 0

    dev = abs(price - vwap) / vwap

    if dev > 0.04:
        return 3

    if dev > 0.025:
        return 2

    if dev > 0.015:
        return 1

    return 0


# ============================================================
# spread compression
# ============================================================

def _spread_compression(row):

    spread = _safe(row.get("spread"))
    spread_avg = _safe(row.get("spread_avg"))

    if spread is None or spread_avg is None or spread_avg == 0:
        return 0

    ratio = spread / spread_avg

    if ratio < 0.5:
        return 2

    if ratio < 0.7:
        return 1

    return 0


# ============================================================
# board speed
# ============================================================

def _board_speed(row):

    speed = _safe(row.get("board_speed"))

    if speed is None:
        return 0

    if speed > 100:
        return 3

    if speed > 60:
        return 2

    if speed > 30:
        return 1

    return 0


# ============================================================
# micro momentum
# ============================================================

def _micro_momentum(row):

    mom = _safe(row.get("micro_momentum"))

    if mom is None:
        return 0

    if abs(mom) > 0.05:
        return 3

    if abs(mom) > 0.03:
        return 2

    if abs(mom) > 0.015:
        return 1

    return 0


# ============================================================
# volume slope (高速アルゴ検出)
# ============================================================

def _volume_slope(row):

    slope = _safe(row.get("volume_slope"))

    if slope is None:
        return 0

    if slope > 5:
        return 3

    if slope > 3:
        return 2

    if slope > 1.5:
        return 1

    return 0


# ============================================================
# main scoring
# ============================================================

def compute_algo_spike_score(row):

    score = 0
    reasons = {}

    funcs = {

        "price_spike": _price_spike,
        "volume_spike": _volume_spike,
        "tick_burst": _tick_burst,
        "vwap_deviation": _vwap_deviation,
        "spread_compression": _spread_compression,
        "board_speed": _board_speed,
        "micro_momentum": _micro_momentum,
        "volume_slope": _volume_slope,

    }

    for name, func in funcs.items():

        try:

            s = func(row)

            if s:
                score += s
                reasons[name] = s

        except Exception:

            logger.exception(f"[ALGO SPIKE] {name} failed")

    return score, reasons


# ============================================================
# detection
# ============================================================

def detect_algo_spike(row):

    score, reasons = compute_algo_spike_score(row)

    if score >= 6:

        return True, score, reasons

    return False, score, reasons


# ============================================================
# DataFrame API
# ============================================================

def apply_algo_spike_ai(df: pd.DataFrame):

    if df is None or df.empty:
        return df

    try:

        df = df.copy()

        scores = []
        flags = []

        for row in df.to_dict("records"):

            detected, score, _ = detect_algo_spike(row)

            scores.append(score)
            flags.append(int(detected))

        df["algo_spike_score"] = scores
        df["flag_algo_spike"] = flags

        logger.info(
            f"[ALGO SPIKE AI] detected={sum(flags)} rows={len(df)}"
        )

        return df

    except Exception:

        logger.exception("[ALGO SPIKE AI] failed")

        return df


# ============================================================
# compatibility API
# ============================================================

def calc_algo_spike_score(row: pd.Series) -> int:
    """
    旧API互換
    """

    vol = row.get("volume")
    vol_slope = row.get("volume_slope")

    try:

        vol = float(vol)
        vol_slope = float(vol_slope)

    except Exception:

        return 0

    score = 0

    if vol_slope > 1.5:
        score += 2

    if vol_slope > 3:
        score += 4

    if vol_slope > 5:
        score += 6

    return score