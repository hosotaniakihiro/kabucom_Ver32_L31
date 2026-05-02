# ============================================================
# File   : trading/indicators/vwap.py
# Version: Ver1.0-PRODUCTION-VWAP-INDICATOR
# ------------------------------------------------------------
# ✔ intraday VWAP
# ✔ VWAP deviation
# ✔ VWAP cross
# ✔ VWAP slope
# ✔ VWAP band
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
# VWAP calculation
# ============================================================

def calc_vwap(df: pd.DataFrame) -> pd.DataFrame:

    df = df.copy()

    close = _safe_numeric(df, "close")
    volume = _safe_numeric(df, "volume")

    typical_price = close

    pv = typical_price * volume

    cumulative_pv = pv.cumsum()
    cumulative_volume = volume.cumsum()

    vwap = cumulative_pv / (cumulative_volume + 1e-10)

    df["vwap"] = vwap

    return df


# ============================================================
# VWAP slope
# ============================================================

def add_vwap_slope(df: pd.DataFrame) -> pd.DataFrame:

    df = df.copy()

    if "vwap" not in df.columns:
        return df

    vwap = pd.to_numeric(df["vwap"], errors="coerce")

    vwap = vwap.replace([np.inf, -np.inf], np.nan)

    df["vwap_slope"] = vwap.diff()

    return df


# ============================================================
# VWAP deviation
# ============================================================

def add_vwap_deviation(df: pd.DataFrame) -> pd.DataFrame:

    df = df.copy()

    if "vwap" not in df.columns:
        return df

    close = _safe_numeric(df, "close")

    vwap = df["vwap"]

    df["vwap_dev"] = (close - vwap) / (vwap + 1e-10)

    return df


# ============================================================
# VWAP bands
# ============================================================

def add_vwap_bands(df: pd.DataFrame, window: int = 20) -> pd.DataFrame:

    df = df.copy()

    if "vwap" not in df.columns:
        return df

    close = _safe_numeric(df, "close")

    dev = close - df["vwap"]

    std = dev.rolling(window, min_periods=5).std()

    df["vwap_upper"] = df["vwap"] + std
    df["vwap_lower"] = df["vwap"] - std

    return df


# ============================================================
# VWAP cross
# ============================================================

def add_vwap_cross(df: pd.DataFrame) -> pd.DataFrame:

    df = df.copy()

    if "vwap" not in df.columns:
        return df

    close = _safe_numeric(df, "close")

    vwap = df["vwap"]

    cross_up = (close > vwap) & (close.shift(1) <= vwap.shift(1))
    cross_down = (close < vwap) & (close.shift(1) >= vwap.shift(1))

    df["vwap_cross_up"] = cross_up.astype(int)
    df["vwap_cross_down"] = cross_down.astype(int)

    return df


# ============================================================
# VWAP strength
# ============================================================

def add_vwap_strength(df: pd.DataFrame) -> pd.DataFrame:

    df = df.copy()

    if "vwap_dev" not in df.columns:
        return df

    dev = pd.to_numeric(df["vwap_dev"], errors="coerce")

    dev = dev.replace([np.inf, -np.inf], np.nan)

    df["vwap_strength"] = dev.abs()

    return df


# ============================================================
# full pipeline
# ============================================================

def apply_vwap_indicators(df: pd.DataFrame) -> pd.DataFrame:

    df = df.copy()

    df = calc_vwap(df)

    df = add_vwap_slope(df)

    df = add_vwap_deviation(df)

    df = add_vwap_bands(df)

    df = add_vwap_cross(df)

    df = add_vwap_strength(df)

    return df