# ============================================================
# trading/ai/tosama_inago_ai.py
# Ver1.2-PRODUCTION-STABLE-SCORE-COMPAT
# ------------------------------------------------------------
# ✔ Ver1.1 機能完全保持（削除ゼロ）
# ✔ scoring_core互換スコアAPI追加
# ✔ detect API互換維持
# ✔ tuple問題解決
# ✔ NaN / inf 完全防御
# ✔ DataFrame API維持
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
# volume spike
# ============================================================

def _volume_spike(row):

    vol = _safe(row.get("volume"))
    vol_avg = _safe(row.get("volume_avg"))

    if vol is None or vol_avg is None or vol_avg == 0:
        return 0

    ratio = vol / vol_avg

    if ratio >= 5:
        return 3
    if ratio >= 3:
        return 2
    if ratio >= 2:
        return 1

    return 0


# ============================================================
# momentum
# ============================================================

def _momentum_score(row):

    slope = _safe(row.get("slope_atr_scaled"))

    if slope is None:
        return 0

    if slope > 5:
        return 3
    if slope > 3:
        return 2
    if slope > 1:
        return 1

    return 0


# ============================================================
# breakout
# ============================================================

def _breakout_score(row):

    if row.get("flag_breakout_high"):
        return 3

    if row.get("flag_volume_price_breakout"):
        return 2

    return 0


# ============================================================
# VWAP
# ============================================================

def _vwap_deviation(row):

    price = _safe(row.get("close_price"))
    vwap = _safe(row.get("vwap"))

    if price is None or vwap is None or vwap == 0:
        return 0

    dev = (price - vwap) / vwap

    if dev > 0.05:
        return 3
    if dev > 0.03:
        return 2
    if dev > 0.015:
        return 1

    return 0


# ============================================================
# ATR
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
# ranking
# ============================================================

def _ranking_momentum(row):

    try:

        rank = int(row.get("rank_position"))
        prev = int(row.get("rank_prev"))

    except Exception:
        return 0

    improvement = prev - rank

    if improvement >= 20:
        return 3
    if improvement >= 10:
        return 2
    if improvement >= 5:
        return 1

    return 0


# ============================================================
# board speed
# ============================================================

def _board_speed(row):

    speed = _safe(row.get("board_speed"))

    if speed is None:
        return 0

    if speed > 50:
        return 3
    if speed > 30:
        return 2
    if speed > 10:
        return 1

    return 0


# ============================================================
# score
# ============================================================

def compute_inago_score(row):

    score = 0
    reasons = {}

    v = _volume_spike(row)
    if v:
        score += v
        reasons["volume_spike"] = v

    m = _momentum_score(row)
    if m:
        score += m
        reasons["momentum"] = m

    b = _breakout_score(row)
    if b:
        score += b
        reasons["breakout"] = b

    vwap = _vwap_deviation(row)
    if vwap:
        score += vwap
        reasons["vwap_deviation"] = vwap

    atr = _atr_volatility(row)
    if atr:
        score += atr
        reasons["atr_volatility"] = atr

    r = _ranking_momentum(row)
    if r:
        score += r
        reasons["ranking_momentum"] = r

    bs = _board_speed(row)
    if bs:
        score += bs
        reasons["board_speed"] = bs

    return score, reasons


# ============================================================
# detection
# ============================================================

def detect_tosama_inago(row):

    score, reasons = compute_inago_score(row)

    if score >= 6:
        return True, score, reasons

    return False, score, reasons


# ============================================================
# scoring_core互換 API（NEW）
# ============================================================

def calc_tosama_inago_score(row) -> int:

    """
    scoring_core 用
    必ず int を返す
    """

    try:

        _, score, _ = detect_tosama_inago(row)

        return int(score)

    except Exception:

        logger.debug("inago score failed")

        return 0


# ============================================================
# DataFrame API
# ============================================================

def apply_inago_ai(df: pd.DataFrame):

    if df is None or df.empty:
        return df

    df = df.copy()

    scores = []
    flags = []

    for _, row in df.iterrows():

        detected, score, _ = detect_tosama_inago(row)

        scores.append(score)
        flags.append(int(detected))

    df["inago_score"] = scores
    df["flag_inago"] = flags

    return df