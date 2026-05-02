# ============================================================
# File   : trading/indicators/ma.py
# Version: Ver1.0-PRODUCTION-MA-CALCULATOR
# ------------------------------------------------------------
# ✔ Simple Moving Average (SMA)
# ✔ Exponential Moving Average (EMA)
# ✔ Multiple MA generator
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
# SMA
# ============================================================

def sma(df: pd.DataFrame, period: int, price_col: str = "close") -> pd.Series:
    """
    Simple Moving Average
    """

    price = _safe_numeric(df, price_col)

    return price.rolling(period, min_periods=1).mean()


# ============================================================
# EMA
# ============================================================

def ema(df: pd.DataFrame, period: int, price_col: str = "close") -> pd.Series:
    """
    Exponential Moving Average
    """

    price = _safe_numeric(df, price_col)

    return price.ewm(span=period, adjust=False).mean()


# ============================================================
# slope
# ============================================================

def ma_slope(series: pd.Series, period: int = 1) -> pd.Series:
    """
    MA slope (price acceleration)
    """

    s = pd.to_numeric(series, errors="coerce")

    s = s.replace([np.inf, -np.inf], np.nan)

    slope = s.diff(period)

    return slope


# ============================================================
# add MA columns
# ============================================================

def add_ma_columns(
    df: pd.DataFrame,
    periods: list[int] = [5, 10, 20, 25, 50, 75],
    price_col: str = "close"
) -> pd.DataFrame:

    df = df.copy()

    for p in periods:

        col = f"ma{p}"

        if col not in df.columns:

            df[col] = sma(df, p, price_col)

    return df


# ============================================================
# add EMA columns
# ============================================================

def add_ema_columns(
    df: pd.DataFrame,
    periods: list[int] = [5, 10, 20, 25, 50, 75],
    price_col: str = "close"
) -> pd.DataFrame:

    df = df.copy()

    for p in periods:

        col = f"ema{p}"

        if col not in df.columns:

            df[col] = ema(df, p, price_col)

    return df


# ============================================================
# add slope columns
# ============================================================

def add_ma_slope_columns(
    df: pd.DataFrame,
    periods: list[int] = [5, 25, 75]
) -> pd.DataFrame:

    df = df.copy()

    for p in periods:

        ma_col = f"ma{p}"

        slope_col = f"ma{p}_slope"

        if ma_col in df.columns and slope_col not in df.columns:

            df[slope_col] = ma_slope(df[ma_col])

    return df


# ============================================================
# full MA pipeline
# ============================================================

def apply_ma_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    全MA計算パイプライン
    """

    df = df.copy()

    df = add_ma_columns(df)
    df = add_ema_columns(df)
    df = add_ma_slope_columns(df)

    return df