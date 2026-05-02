# ============================================================
# File   : trading/indicators/rsi.py
# Version: Ver1.0-PRODUCTION-RSI-INDICATOR
# ------------------------------------------------------------
# ✔ RSI calculation
# ✔ slope
# ✔ overbought / oversold detection
# ✔ cross detection
# ✔ momentum strength
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

def _safe_numeric(df: pd.DataFrame, col: str) -> pd.Series:

    if col not in df.columns:
        return pd.Series(index=df.index, dtype="float64")

    s = pd.to_numeric(df[col], errors="coerce")

    s = s.replace([np.inf, -np.inf], np.nan)

    return s


# ============================================================
# RSI calculation
# ============================================================

def calc_rsi(
    df: pd.DataFrame,
    period: int = 14,
    price_col: str = "close"
) -> pd.DataFrame:

    df = df.copy()

    price = _safe_numeric(df, price_col)

    delta = price.diff()

    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.rolling(period, min_periods=period).mean()
    avg_loss = loss.rolling(period, min_periods=period).mean()

    rs = avg_gain / (avg_loss + 1e-10)

    rsi = 100 - (100 / (1 + rs))

    df["rsi"] = rsi

    return df


# ============================================================
# slope
# ============================================================

def add_rsi_slope(df: pd.DataFrame) -> pd.DataFrame:

    df = df.copy()

    if "rsi" not in df.columns:
        return df

    rsi = pd.to_numeric(df["rsi"], errors="coerce")

    rsi = rsi.replace([np.inf, -np.inf], np.nan)

    df["rsi_slope"] = rsi.diff()

    return df


# ============================================================
# overbought / oversold
# ============================================================

def add_rsi_levels(
    df: pd.DataFrame,
    overbought: int = 70,
    oversold: int = 30
) -> pd.DataFrame:

    df = df.copy()

    if "rsi" not in df.columns:
        return df

    rsi = df["rsi"]

    df["rsi_overbought"] = (rsi >= overbought).astype(int)
    df["rsi_oversold"] = (rsi <= oversold).astype(int)

    return df


# ============================================================
# cross detection
# ============================================================

def add_rsi_cross(df: pd.DataFrame) -> pd.DataFrame:

    df = df.copy()

    if "rsi" not in df.columns:
        return df

    rsi = df["rsi"]

    cross_50_up = (rsi > 50) & (rsi.shift(1) <= 50)
    cross_50_down = (rsi < 50) & (rsi.shift(1) >= 50)

    cross_30_up = (rsi > 30) & (rsi.shift(1) <= 30)
    cross_70_down = (rsi < 70) & (rsi.shift(1) >= 70)

    df["rsi_cross_50_up"] = cross_50_up.astype(int)
    df["rsi_cross_50_down"] = cross_50_down.astype(int)
    df["rsi_cross_30_up"] = cross_30_up.astype(int)
    df["rsi_cross_70_down"] = cross_70_down.astype(int)

    return df


# ============================================================
# momentum strength
# ============================================================

def add_rsi_strength(df: pd.DataFrame) -> pd.DataFrame:

    df = df.copy()

    if "rsi" not in df.columns:
        return df

    rsi = pd.to_numeric(df["rsi"], errors="coerce")

    rsi = rsi.replace([np.inf, -np.inf], np.nan)

    df["rsi_strength"] = (rsi - 50).abs()

    return df


# ============================================================
# full pipeline
# ============================================================

def apply_rsi_indicators(df: pd.DataFrame) -> pd.DataFrame:

    df = df.copy()

    df = calc_rsi(df)

    df = add_rsi_slope(df)

    df = add_rsi_levels(df)

    df = add_rsi_cross(df)

    df = add_rsi_strength(df)

    return df