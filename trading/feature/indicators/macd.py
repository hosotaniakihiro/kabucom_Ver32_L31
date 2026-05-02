# ============================================================
# File   : trading/indicators/macd.py
# Version: Ver1.0-PRODUCTION-MACD-INDICATOR
# ------------------------------------------------------------
# ✔ MACD
# ✔ Signal
# ✔ Histogram
# ✔ slope calculation
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
# EMA
# ============================================================

def _ema(series: pd.Series, span: int):

    return series.ewm(span=span, adjust=False).mean()


# ============================================================
# MACD calculation
# ============================================================

def calc_macd(
    df: pd.DataFrame,
    price_col: str = "close",
    fast: int = 12,
    slow: int = 26,
    signal: int = 9
) -> pd.DataFrame:

    df = df.copy()

    price = _safe_numeric(df, price_col)

    ema_fast = _ema(price, fast)
    ema_slow = _ema(price, slow)

    macd = ema_fast - ema_slow

    signal_line = _ema(macd, signal)

    histogram = macd - signal_line

    df["macd"] = macd
    df["macd_signal"] = signal_line
    df["macd_hist"] = histogram

    return df


# ============================================================
# slope
# ============================================================

def _slope(series: pd.Series):

    s = pd.to_numeric(series, errors="coerce")

    s = s.replace([np.inf, -np.inf], np.nan)

    return s.diff()


# ============================================================
# slope columns
# ============================================================

def add_macd_slopes(df: pd.DataFrame) -> pd.DataFrame:

    df = df.copy()

    if "macd" in df.columns:
        df["macd_slope"] = _slope(df["macd"])

    if "macd_hist" in df.columns:
        df["macd_hist_slope"] = _slope(df["macd_hist"])

    return df


# ============================================================
# MACD cross
# ============================================================

def add_macd_cross(df: pd.DataFrame) -> pd.DataFrame:

    df = df.copy()

    if "macd" not in df.columns or "macd_signal" not in df.columns:
        return df

    macd = df["macd"]
    signal = df["macd_signal"]

    cross_up = (macd > signal) & (macd.shift(1) <= signal.shift(1))
    cross_down = (macd < signal) & (macd.shift(1) >= signal.shift(1))

    df["macd_cross_up"] = cross_up.astype(int)
    df["macd_cross_down"] = cross_down.astype(int)

    return df


# ============================================================
# MACD strength
# ============================================================

def add_macd_strength(df: pd.DataFrame) -> pd.DataFrame:

    df = df.copy()

    if "macd_hist" not in df.columns:
        return df

    hist = pd.to_numeric(df["macd_hist"], errors="coerce")

    hist = hist.replace([np.inf, -np.inf], np.nan)

    df["macd_strength"] = hist.abs()

    return df


# ============================================================
# full pipeline
# ============================================================

def apply_macd_indicators(df: pd.DataFrame) -> pd.DataFrame:

    df = df.copy()

    df = calc_macd(df)

    df = add_macd_slopes(df)

    df = add_macd_cross(df)

    df = add_macd_strength(df)

    return df