# ============================================================
# File   : trading/features/volume/volume_trend.py
# Version: Ver1.0-PRODUCTION-VOLUME-TREND
# ------------------------------------------------------------
# ✔ volume trend detection
# ✔ volume momentum
# ✔ volume contraction / expansion
# ✔ up-volume / down-volume
# ✔ volume pressure
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
# volume moving averages
# ============================================================

def add_volume_ma(df: pd.DataFrame):

    df = df.copy()

    volume = _safe_numeric(df, "volume")

    df["volume_ma5"] = volume.rolling(5, min_periods=2).mean()
    df["volume_ma20"] = volume.rolling(20, min_periods=5).mean()

    return df


# ============================================================
# volume trend
# ============================================================

def add_volume_trend(df: pd.DataFrame):

    df = df.copy()

    if "volume_ma5" not in df.columns:
        df = add_volume_ma(df)

    ma5 = df["volume_ma5"]
    ma20 = df["volume_ma20"]

    trend_up = ma5 > ma20
    trend_down = ma5 < ma20

    df["volume_trend_up"] = trend_up.astype(int)
    df["volume_trend_down"] = trend_down.astype(int)

    return df


# ============================================================
# volume momentum
# ============================================================

def add_volume_momentum(df: pd.DataFrame):

    df = df.copy()

    volume = _safe_numeric(df, "volume")

    momentum = volume.diff()

    df["volume_momentum"] = momentum

    signal = momentum > momentum.rolling(10, min_periods=3).mean()

    df["volume_momentum_signal"] = signal.astype(int)

    return df


# ============================================================
# volume expansion / contraction
# ============================================================

def add_volume_expansion(df: pd.DataFrame):

    df = df.copy()

    if "volume_ma20" not in df.columns:
        df = add_volume_ma(df)

    volume = _safe_numeric(df, "volume")
    ma20 = df["volume_ma20"]

    expansion = volume > ma20 * 1.3
    contraction = volume < ma20 * 0.7

    df["volume_expansion"] = expansion.astype(int)
    df["volume_contraction"] = contraction.astype(int)

    return df


# ============================================================
# up-volume / down-volume
# ============================================================

def add_volume_direction(df: pd.DataFrame):

    df = df.copy()

    close = _safe_numeric(df, "close")
    volume = _safe_numeric(df, "volume")

    up = close > close.shift(1)
    down = close < close.shift(1)

    df["up_volume"] = volume.where(up, 0)
    df["down_volume"] = volume.where(down, 0)

    return df


# ============================================================
# volume pressure
# ============================================================

def add_volume_pressure(df: pd.DataFrame):

    df = df.copy()

    if "up_volume" not in df.columns:
        df = add_volume_direction(df)

    up = _safe_numeric(df, "up_volume")
    down = _safe_numeric(df, "down_volume")

    pressure = up - down

    df["volume_pressure"] = pressure

    signal = pressure > pressure.rolling(10, min_periods=3).mean()

    df["buying_pressure"] = signal.astype(int)

    return df


# ============================================================
# volume trend strength
# ============================================================

def add_volume_trend_strength(df: pd.DataFrame):

    df = df.copy()

    score = 0

    cols = [
        "volume_trend_up",
        "volume_expansion",
        "volume_momentum_signal",
        "buying_pressure"
    ]

    for c in cols:

        if c in df.columns:
            score += df[c]

    df["volume_trend_strength"] = score

    return df


# ============================================================
# full pipeline
# ============================================================

def apply_volume_trend_features(df: pd.DataFrame):

    df = df.copy()

    df = add_volume_ma(df)

    df = add_volume_trend(df)

    df = add_volume_momentum(df)

    df = add_volume_expansion(df)

    df = add_volume_direction(df)

    df = add_volume_pressure(df)

    df = add_volume_trend_strength(df)

    return df