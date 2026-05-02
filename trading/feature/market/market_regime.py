# ============================================================
# File   : feature/market/market_regime.py
# Version: Ver1.0-PRODUCTION-MARKET-REGIME
# ------------------------------------------------------------
# ✔ market trend detection
# ✔ volatility regime
# ✔ bull / bear market
# ✔ trend vs range
# ✔ risk off detection
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
# market trend
# ------------------------------------------------------------

def add_market_trend(df: pd.DataFrame):

    df = df.copy()

    close = _safe_numeric(df, "close")

    ma50 = close.rolling(50, min_periods=10).mean()
    ma200 = close.rolling(200, min_periods=20).mean()

    bull = ma50 > ma200
    bear = ma50 < ma200

    df["market_bull"] = bull.astype(int)
    df["market_bear"] = bear.astype(int)

    return df


# ------------------------------------------------------------
# volatility regime
# ------------------------------------------------------------

def add_volatility_regime(df: pd.DataFrame):

    df = df.copy()

    high = _safe_numeric(df, "high")
    low = _safe_numeric(df, "low")

    tr = high - low

    vol = tr.rolling(20, min_periods=5).mean()

    vol_ma = vol.rolling(50, min_periods=10).mean()

    high_vol = vol > vol_ma * 1.5
    low_vol = vol < vol_ma * 0.7

    df["high_volatility"] = high_vol.astype(int)
    df["low_volatility"] = low_vol.astype(int)

    return df


# ------------------------------------------------------------
# trend vs range
# ------------------------------------------------------------

def add_trend_range_regime(df: pd.DataFrame):

    df = df.copy()

    close = _safe_numeric(df, "close")

    ma20 = close.rolling(20, min_periods=5).mean()

    slope = ma20.diff()

    trend = slope.abs() > ma20 * 0.001

    df["trend_market"] = trend.astype(int)
    df["range_market"] = (~trend).astype(int)

    return df


# ------------------------------------------------------------
# risk off detection
# ------------------------------------------------------------

def add_risk_off(df: pd.DataFrame):

    df = df.copy()

    close = _safe_numeric(df, "close")

    drop = close.pct_change()

    risk_off = drop < -0.02

    df["risk_off"] = risk_off.astype(int)

    return df


# ------------------------------------------------------------
# regime score
# ------------------------------------------------------------

def add_market_regime_score(df: pd.DataFrame):

    df = df.copy()

    score = 0

    cols = [
        "market_bull",
        "high_volatility",
        "trend_market"
    ]

    for c in cols:

        if c in df.columns:
            score += df[c]

    df["market_regime_score"] = score

    return df


# ------------------------------------------------------------
# full pipeline
# ------------------------------------------------------------

def apply_market_regime_features(df: pd.DataFrame):

    df = df.copy()

    df = add_market_trend(df)

    df = add_volatility_regime(df)

    df = add_trend_range_regime(df)

    df = add_risk_off(df)

    df = add_market_regime_score(df)

    return df