# ============================================================
# File   : feature/price/price_acceleration.py
# Version: Ver1.0-PRODUCTION-PRICE-ACCELERATION
# ------------------------------------------------------------
# ✔ price velocity
# ✔ price acceleration
# ✔ price expansion
# ✔ momentum breakout
# ✔ impulse detection
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
# price velocity
# ------------------------------------------------------------

def add_price_velocity(df: pd.DataFrame):

    df = df.copy()

    close = _safe_numeric(df, "close")

    velocity = close.diff()

    df["price_velocity"] = velocity

    return df


# ------------------------------------------------------------
# price acceleration
# ------------------------------------------------------------

def add_price_acceleration(df: pd.DataFrame):

    df = df.copy()

    if "price_velocity" not in df.columns:
        df = add_price_velocity(df)

    vel = _safe_numeric(df, "price_velocity")

    acceleration = vel.diff()

    df["price_acceleration"] = acceleration

    return df


# ------------------------------------------------------------
# price expansion
# ------------------------------------------------------------

def add_price_expansion(df: pd.DataFrame):

    df = df.copy()

    high = _safe_numeric(df, "high")
    low = _safe_numeric(df, "low")

    range_ = high - low

    avg_range = range_.rolling(20, min_periods=5).mean()

    expansion = range_ > avg_range * 1.5

    df["price_expansion"] = expansion.astype(int)

    return df


# ------------------------------------------------------------
# impulse move detection
# ------------------------------------------------------------

def add_price_impulse(df: pd.DataFrame):

    df = df.copy()

    close = _safe_numeric(df, "close")

    move = close.diff()

    move_std = move.rolling(20, min_periods=5).std()

    impulse = move > move_std * 2

    df["price_impulse"] = impulse.astype(int)

    return df


# ------------------------------------------------------------
# momentum breakout
# ------------------------------------------------------------

def add_momentum_breakout(df: pd.DataFrame):

    df = df.copy()

    close = _safe_numeric(df, "close")

    prev_high = close.rolling(20, min_periods=5).max().shift(1)

    breakout = close > prev_high

    df["momentum_breakout"] = breakout.astype(int)

    return df


# ------------------------------------------------------------
# acceleration strength
# ------------------------------------------------------------

def add_price_acceleration_strength(df: pd.DataFrame):

    df = df.copy()

    score = 0

    cols = [
        "price_expansion",
        "price_impulse",
        "momentum_breakout"
    ]

    for c in cols:

        if c in df.columns:
            score += df[c]

    df["price_acceleration_strength"] = score

    return df


# ------------------------------------------------------------
# full pipeline
# ------------------------------------------------------------

def apply_price_acceleration_features(df: pd.DataFrame):

    df = df.copy()

    df = add_price_velocity(df)

    df = add_price_acceleration(df)

    df = add_price_expansion(df)

    df = add_price_impulse(df)

    df = add_momentum_breakout(df)

    df = add_price_acceleration_strength(df)

    return df