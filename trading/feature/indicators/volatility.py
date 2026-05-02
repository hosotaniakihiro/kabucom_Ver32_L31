# ============================================================
# File   : trading/indicators/volatility.py
# Version: Ver1.0-PRODUCTION-VOLATILITY-ENGINE
# ------------------------------------------------------------
# ✔ True Range
# ✔ ATR
# ✔ volatility expansion
# ✔ volatility squeeze
# ✔ volatility spike
# ✔ price acceleration
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
# True Range
# ============================================================

def add_true_range(df: pd.DataFrame):

    df = df.copy()

    high = _safe_numeric(df, "high")
    low = _safe_numeric(df, "low")
    close = _safe_numeric(df, "close")

    prev_close = close.shift(1)

    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()

    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    df["true_range"] = tr

    return df


# ============================================================
# ATR
# ============================================================

def add_atr(df, period=14):

    high = df["high_price"]
    low = df["low_price"]
    close = df["close_price"]

    tr1 = high - low
    tr2 = (high - close.shift()).abs()
    tr3 = (low - close.shift()).abs()

    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    df["atr_1m"] = tr.rolling(period).mean()

    return df

# ============================================================
# volatility expansion
# ============================================================

def add_volatility_expansion(df: pd.DataFrame):

    df = df.copy()

    if "atr" not in df.columns:
        df = add_atr(df)

    atr = df["atr"]

    atr_ma = atr.rolling(20, min_periods=5).mean()

    expansion = atr > atr_ma * 1.3

    df["volatility_expansion"] = expansion.astype(int)

    return df


# ============================================================
# volatility squeeze
# ============================================================

def add_volatility_squeeze(df: pd.DataFrame):

    df = df.copy()

    if "atr" not in df.columns:
        df = add_atr(df)

    atr = df["atr"]

    atr_ma = atr.rolling(20, min_periods=5).mean()

    squeeze = atr < atr_ma * 0.7

    df["volatility_squeeze"] = squeeze.astype(int)

    return df


# ============================================================
# volatility spike
# ============================================================

def add_volatility_spike(df: pd.DataFrame):

    df = df.copy()

    if "true_range" not in df.columns:
        df = add_true_range(df)

    tr = df["true_range"]

    tr_ma = tr.rolling(20, min_periods=5).mean()

    spike = tr > tr_ma * 2

    df["volatility_spike"] = spike.astype(int)

    return df


# ============================================================
# price acceleration
# ============================================================

def add_price_acceleration(df: pd.DataFrame):

    df = df.copy()

    close = _safe_numeric(df, "close")

    velocity = close.diff()

    acceleration = velocity.diff()

    df["price_velocity"] = velocity
    df["price_acceleration"] = acceleration

    accel_signal = acceleration > acceleration.rolling(10).mean()

    df["acceleration_signal"] = accel_signal.astype(int)

    return df


# ============================================================
# volatility strength
# ============================================================

def add_volatility_strength(df: pd.DataFrame):

    df = df.copy()

    score = 0

    cols = [
        "volatility_expansion",
        "volatility_spike",
        "acceleration_signal"
    ]

    for c in cols:

        if c in df.columns:
            score += df[c]

    df["volatility_strength"] = score

    return df


# ============================================================
# full pipeline
# ============================================================

def apply_volatility_indicators(df: pd.DataFrame):

    df = df.copy()

    df = add_true_range(df)

    df = add_atr(df)

    df = add_volatility_expansion(df)

    df = add_volatility_squeeze(df)

    df = add_volatility_spike(df)

    df = add_price_acceleration(df)

    df = add_volatility_strength(df)

    return df