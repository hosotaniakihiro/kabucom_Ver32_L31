# ============================================================
# File   : trading/feature/volume/volume_spike.py
# Version: Ver1.0-PRODUCTION-VOLUME-SPIKE-DETECTOR
# ------------------------------------------------------------
# ✔ volume spike detection
# ✔ relative volume (RVOL)
# ✔ volume acceleration
# ✔ volume breakout
# ✔ institutional flow detection
# ✔ NaN / inf safe
# ✔ vectorized
# ✔ production ready
# ============================================================

from __future__ import annotations

import logging
import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)


# ============================================================
# safe numeric
# ============================================================

def _safe_numeric(df: pd.DataFrame, col: str):

    if col not in df.columns:
        return pd.Series(index=df.index, dtype="float64")

    s = pd.to_numeric(df[col], errors="coerce")

    s = s.replace([np.inf, -np.inf], np.nan)

    return s


# ============================================================
# volume moving average
# ============================================================

def add_volume_ma(df: pd.DataFrame, window: int = 20):

    df = df.copy()

    volume = _safe_numeric(df, "volume")

    df["volume_ma"] = volume.rolling(window, min_periods=5).mean()

    return df


# ============================================================
# relative volume
# ============================================================

def add_relative_volume(df: pd.DataFrame):

    df = df.copy()

    if "volume_ma" not in df.columns:
        df = add_volume_ma(df)

    volume = _safe_numeric(df, "volume")

    vol_ma = df["volume_ma"]

    rvol = volume / (vol_ma + 1e-10)

    df["rvol"] = rvol

    return df


# ============================================================
# volume spike
# ============================================================

def add_volume_spike(df: pd.DataFrame, multiplier: float = 2.0):

    df = df.copy()

    if "volume_ma" not in df.columns:
        df = add_volume_ma(df)

    volume = _safe_numeric(df, "volume")

    vol_ma = df["volume_ma"]

    spike = volume > vol_ma * multiplier

    df["volume_spike"] = spike.astype(int)

    return df


# ============================================================
# volume acceleration
# ============================================================

def add_volume_acceleration(df: pd.DataFrame):

    df = df.copy()

    volume = _safe_numeric(df, "volume")

    velocity = volume.diff()

    acceleration = velocity.diff()

    df["volume_velocity"] = velocity
    df["volume_acceleration"] = acceleration

    accel_signal = acceleration > acceleration.rolling(10).mean()

    df["volume_accel_signal"] = accel_signal.astype(int)

    return df


# ============================================================
# volume breakout
# ============================================================

def add_volume_breakout(df: pd.DataFrame):

    df = df.copy()

    volume = _safe_numeric(df, "volume")

    prev_high = volume.rolling(30, min_periods=5).max().shift(1)

    breakout = volume > prev_high

    df["volume_breakout"] = breakout.astype(int)

    return df


# ============================================================
# institutional flow
# ============================================================

def add_institutional_flow(df: pd.DataFrame):

    df = df.copy()

    if "rvol" not in df.columns:
        df = add_relative_volume(df)

    rvol = df["rvol"]

    flow = rvol > 3

    df["institutional_flow"] = flow.astype(int)

    return df


# ============================================================
# volume strength
# ============================================================

def add_volume_strength(df: pd.DataFrame):

    df = df.copy()

    score = 0

    cols = [
        "volume_spike",
        "volume_breakout",
        "volume_accel_signal",
        "institutional_flow"
    ]

    for c in cols:

        if c in df.columns:
            score += df[c]

    df["volume_strength"] = score

    return df


# ============================================================
# full pipeline
# ============================================================

def apply_volume_indicators(df: pd.DataFrame):

    df = df.copy()

    df = add_volume_ma(df)

    df = add_relative_volume(df)

    df = add_volume_spike(df)

    df = add_volume_acceleration(df)

    df = add_volume_breakout(df)

    df = add_institutional_flow(df)

    df = add_volume_strength(df)

    return df