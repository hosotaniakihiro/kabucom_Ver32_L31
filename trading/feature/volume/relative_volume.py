# ============================================================
# File   : feature/volume/relative_volume.py
# Version: Ver1.0-PRODUCTION-RELATIVE-VOLUME
# ------------------------------------------------------------
# ✔ relative volume (RVOL)
# ✔ volume anomaly
# ✔ volume surge
# ✔ unusual activity detection
# ✔ volume pressure ratio
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
# relative volume (RVOL)
# ------------------------------------------------------------

def add_relative_volume(df: pd.DataFrame):

    df = df.copy()

    volume = _safe_numeric(df, "volume")

    vol_ma = volume.rolling(20, min_periods=5).mean()

    rvol = volume / (vol_ma + 1e-9)

    df["relative_volume"] = rvol

    return df


# ------------------------------------------------------------
# volume anomaly
# ------------------------------------------------------------

def add_volume_anomaly(df: pd.DataFrame):

    df = df.copy()

    if "relative_volume" not in df.columns:
        df = add_relative_volume(df)

    anomaly = df["relative_volume"] > 2.0

    df["volume_anomaly"] = anomaly.astype(int)

    return df


# ------------------------------------------------------------
# volume surge
# ------------------------------------------------------------

def add_volume_surge(df: pd.DataFrame):

    df = df.copy()

    volume = _safe_numeric(df, "volume")

    vol_std = volume.rolling(20, min_periods=5).std()

    vol_ma = volume.rolling(20, min_periods=5).mean()

    surge = volume > (vol_ma + vol_std * 2)

    df["volume_surge"] = surge.astype(int)

    return df


# ------------------------------------------------------------
# volume pressure ratio
# ------------------------------------------------------------

def add_volume_pressure_ratio(df: pd.DataFrame):

    df = df.copy()

    close = _safe_numeric(df, "close")
    volume = _safe_numeric(df, "volume")

    up = close > close.shift(1)
    down = close < close.shift(1)

    up_vol = volume.where(up, 0)
    down_vol = volume.where(down, 0)

    up_sum = up_vol.rolling(10, min_periods=3).sum()
    down_sum = down_vol.rolling(10, min_periods=3).sum()

    ratio = up_sum / (down_sum + 1e-9)

    df["volume_pressure_ratio"] = ratio

    return df


# ------------------------------------------------------------
# relative volume strength
# ------------------------------------------------------------

def add_relative_volume_strength(df: pd.DataFrame):

    df = df.copy()

    score = 0

    cols = [
        "volume_anomaly",
        "volume_surge"
    ]

    for c in cols:

        if c in df.columns:
            score += df[c]

    df["relative_volume_strength"] = score

    return df


# ------------------------------------------------------------
# full pipeline
# ------------------------------------------------------------

def apply_relative_volume_features(df: pd.DataFrame):

    df = df.copy()

    df = add_relative_volume(df)

    df = add_volume_anomaly(df)

    df = add_volume_surge(df)

    df = add_volume_pressure_ratio(df)

    df = add_relative_volume_strength(df)

    return df