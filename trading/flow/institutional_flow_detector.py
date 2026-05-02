# ============================================================
# File   : trading/flow/institutional_flow_detector.py
# Version: Ver1.0-INSTITUTIONAL-FLOW-DETECTOR
# ------------------------------------------------------------
# ✔ institutional money flow detection
# ✔ volume expansion
# ✔ VWAP breakout
# ✔ momentum acceleration
# ✔ ranking velocity
# ✔ liquidity score
# ✔ vectorized
# ✔ production safe
# ============================================================

from __future__ import annotations

import logging
import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)


# ============================================================
# volume expansion
# ============================================================

def _volume_expansion(df):

    if "volume" not in df.columns:
        df["volume_expansion"] = 0
        return df

    vol_ma = df["volume"].rolling(20).mean()

    df["volume_expansion"] = df["volume"] / (vol_ma + 1e-9)

    return df


# ============================================================
# vwap breakout
# ============================================================

def _vwap_breakout(df):

    if "close" not in df.columns or "vwap" not in df.columns:
        df["vwap_breakout"] = 0
        return df

    df["vwap_breakout"] = (
        (df["close"] - df["vwap"]) /
        (df["vwap"] + 1e-9)
    )

    return df


# ============================================================
# price acceleration
# ============================================================

def _price_acceleration(df):

    if "close" not in df.columns:
        df["price_accel"] = 0
        return df

    df["price_accel"] = df["close"].diff().diff()

    return df


# ============================================================
# ranking velocity
# ============================================================

def _ranking_velocity(df):

    if "rank_velocity" not in df.columns:
        df["rank_velocity"] = 0

    return df


# ============================================================
# liquidity score
# ============================================================

def _liquidity(df):

    if "turnover" not in df.columns:
        df["liquidity_score"] = 0
        return df

    df["liquidity_score"] = np.log1p(df["turnover"])

    return df


# ============================================================
# institutional flow score
# ============================================================

def _flow_score(df):

    df["institutional_flow_score"] = (
        df["volume_expansion"] * 30 +
        df["vwap_breakout"] * 40 +
        df["price_accel"] * 10 +
        df["rank_velocity"] * 20 +
        df["liquidity_score"] * 5
    )

    return df


# ============================================================
# main engine
# ============================================================

def apply_institutional_flow(df: pd.DataFrame) -> pd.DataFrame:

    try:

        if df is None or df.empty:
            return df

        df = df.copy()

        df = _volume_expansion(df)
        df = _vwap_breakout(df)
        df = _price_acceleration(df)
        df = _ranking_velocity(df)
        df = _liquidity(df)

        df = _flow_score(df)

        return df

    except Exception:

        logger.exception("[institutional_flow] failed")

        return df