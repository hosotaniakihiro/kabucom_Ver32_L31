# ============================================================
# File   : feature/volume/distribution.py
# Version: Ver1.0-PRODUCTION-DISTRIBUTION-DETECTOR
# ------------------------------------------------------------
# ✔ distribution detection
# ✔ selling pressure
# ✔ volume spike sell
# ✔ price rejection
# ✔ distribution strength
# ✔ NaN / inf safe
# ✔ vectorized
# ============================================================

from __future__ import annotations

import logging
import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)


# ------------------------------------------------------------
# safe numeric
# ------------------------------------------------------------

def _safe_numeric(df: pd.DataFrame, col: str):

    if col not in df.columns:
        return pd.Series(index=df.index, dtype="float64")

    s = pd.to_numeric(df[col], errors="coerce")

    s = s.replace([np.inf, -np.inf], np.nan)

    return s


# ------------------------------------------------------------
# selling pressure
# ------------------------------------------------------------

def add_selling_pressure(df: pd.DataFrame):

    df = df.copy()

    close = _safe_numeric(df, "close")
    open_ = _safe_numeric(df, "open")

    sell = close < open_

    df["selling_pressure"] = sell.astype(int)

    return df


# ------------------------------------------------------------
# volume spike sell
# ------------------------------------------------------------

def add_volume_spike_sell(df: pd.DataFrame):

    df = df.copy()

    volume = _safe_numeric(df, "volume")

    vol_ma = volume.rolling(20, min_periods=5).mean()

    spike = volume > vol_ma * 1.5

    df["volume_spike_sell"] = spike.astype(int)

    return df


# ------------------------------------------------------------
# price rejection
# ------------------------------------------------------------

def add_price_rejection(df: pd.DataFrame):

    df = df.copy()

    high = _safe_numeric(df, "high")
    close = _safe_numeric(df, "close")
    low = _safe_numeric(df, "low")

    upper_wick = high - close
    candle = high - low

    rejection = upper_wick > candle * 0.5

    df["price_rejection"] = rejection.astype(int)

    return df


# ------------------------------------------------------------
# distribution detection
# ------------------------------------------------------------

def add_distribution(df: pd.DataFrame):

    df = df.copy()

    if "selling_pressure" not in df.columns:
        df = add_selling_pressure(df)

    if "volume_spike_sell" not in df.columns:
        df = add_volume_spike_sell(df)

    if "price_rejection" not in df.columns:
        df = add_price_rejection(df)

    dist = (
        (df["selling_pressure"] == 1) &
        (df["volume_spike_sell"] == 1)
    )

    df["distribution"] = dist.astype(int)

    return df


# ------------------------------------------------------------
# distribution strength
# ------------------------------------------------------------

def add_distribution_strength(df: pd.DataFrame):

    df = df.copy()

    score = 0

    cols = [
        "selling_pressure",
        "volume_spike_sell",
        "price_rejection",
        "distribution"
    ]

    for c in cols:

        if c in df.columns:
            score += df[c]

    df["distribution_strength"] = score

    return df


# ------------------------------------------------------------
# full pipeline
# ------------------------------------------------------------

def apply_distribution_features(df: pd.DataFrame):

    df = df.copy()

    df = add_selling_pressure(df)

    df = add_volume_spike_sell(df)

    df = add_price_rejection(df)

    df = add_distribution(df)

    df = add_distribution_strength(df)

    return df