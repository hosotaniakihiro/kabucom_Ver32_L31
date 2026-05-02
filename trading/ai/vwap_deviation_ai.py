# ============================================================
# File   : trading/ai/vwap_deviation_ai.py
# Version: VWAP-DEVIATION-AI-PRODUCTION-STABLE
# ------------------------------------------------------------
# ✔ VWAP乖離AI
# ✔ mean reversion
# ✔ overextension detection
# ✔ VWAP trend analysis
# ✔ volume confirmation
# ✔ ATR volatility
# ✔ Bollinger position
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
# VWAP deviation
# ============================================================

def _vwap_deviation(row):

    price = _safe(row.get("close_price"))
    vwap = _safe(row.get("vwap"))

    if price is None or vwap is None or vwap == 0:
        return 0

    return (price - vwap) / vwap


# ============================================================
# VWAP slope
# ============================================================

def _vwap_slope(row):

    slope = _safe(row.get("vwap_slope"))

    if slope is None:
        return 0

    if slope > 0.002:
        return 1

    if slope < -0.002:
        return -1

    return 0


# ============================================================
# volume confirmation
# ============================================================

def _volume_confirmation(row):

    vol = _safe(row.get("volume"))
    vol_avg = _safe(row.get("volume_avg"))

    if vol is None or vol_avg is None or vol_avg == 0:
        return 0

    ratio = vol / vol_avg

    if ratio > 3:
        return 2

    if ratio > 2:
        return 1

    return 0


# ============================================================
# ATR volatility
# ============================================================

def _atr_volatility(row):

    atr = _safe(row.get("atr"))
    atr_avg = _safe(row.get("atr_avg"))

    if atr is None or atr_avg is None or atr_avg == 0:
        return 0

    ratio = atr / atr_avg

    if ratio > 2:
        return 2

    if ratio > 1.5:
        return 1

    return 0


# ============================================================
# Bollinger position
# ============================================================

def _bollinger_position(row):

    price = _safe(row.get("close_price"))
    upper = _safe(row.get("bb_upper"))
    lower = _safe(row.get("bb_lower"))

    if price is None or upper is None or lower is None:
        return 0

    if price > upper:
        return 2

    if price < lower:
        return -2

    return 0


# ============================================================
# compute deviation score
# ============================================================

def compute_vwap_deviation_score(row):

    score = 0
    reasons = {}

    dev = _vwap_deviation(row)

    if dev > 0.05:

        score -= 3
        reasons["vwap_overextension"] = -3

    elif dev > 0.03:

        score -= 2
        reasons["vwap_extension"] = -2

    elif dev < -0.05:

        score += 3
        reasons["vwap_oversold"] = 3

    elif dev < -0.03:

        score += 2
        reasons["vwap_discount"] = 2


    slope = _vwap_slope(row)

    if slope > 0:

        score += 1
        reasons["vwap_uptrend"] = 1

    if slope < 0:

        score -= 1
        reasons["vwap_downtrend"] = -1


    vol = _volume_confirmation(row)

    if vol:

        score += vol
        reasons["volume_confirmation"] = vol


    atr = _atr_volatility(row)

    if atr:

        score += atr
        reasons["atr_volatility"] = atr


    bb = _bollinger_position(row)

    if bb:

        score += bb
        reasons["bollinger_position"] = bb


    return score, reasons


# ============================================================
# signal classification
# ============================================================

def classify_vwap_signal(row):

    score, reasons = compute_vwap_deviation_score(row)

    if score >= 3:

        return "VWAP_REVERSION_LONG", score, reasons

    if score <= -3:

        return "VWAP_REVERSION_SHORT", score, reasons

    if score > 0:

        return "VWAP_TREND_LONG", score, reasons

    if score < 0:

        return "VWAP_TREND_SHORT", score, reasons

    return "VWAP_NEUTRAL", score, reasons


# ============================================================
# DataFrame API
# ============================================================

def apply_vwap_deviation_ai(df: pd.DataFrame):

    if df is None or df.empty:
        return df

    try:

        df = df.copy()

        scores = []
        signals = []

        for row in df.to_dict("records"):

            signal, score, _ = classify_vwap_signal(row)

            scores.append(score)
            signals.append(signal)

        df["vwap_deviation_score"] = scores
        df["vwap_signal"] = signals

        logger.info(
            f"[VWAP AI] processed rows={len(df)}"
        )

        return df

    except Exception:

        logger.exception("[VWAP AI] failed")

        return df


# ============================================================
# compatibility API
# ============================================================

def calc_vwap_deviation_score(row: pd.Series) -> int:

    close = row.get("close_price")
    vwap = row.get("vwap")

    try:

        close = float(close)
        vwap = float(vwap)

    except Exception:

        return 0

    if vwap == 0:
        return 0

    dev = (close - vwap) / vwap

    score = 0

    if dev > 0.003:
        score += 2

    if dev > 0.007:
        score += 4

    if dev > 0.015:
        score += 6

    return score