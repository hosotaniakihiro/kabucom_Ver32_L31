# ============================================================
# File   : trading/signals/breakout.py
# Version: Ver1.0-PRODUCTION-BREAKOUT-DETECTOR
# ------------------------------------------------------------
# ✔ range breakout
# ✔ recent high breakout
# ✔ volatility breakout
# ✔ VWAP breakout
# ✔ volume confirmation
# ✔ fake breakout filter
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
# range breakout
# ============================================================

def add_range_breakout(df: pd.DataFrame, window: int = 20):

    df = df.copy()

    high = _safe_numeric(df, "high")
    close = _safe_numeric(df, "close")

    range_high = high.rolling(window, min_periods=5).max().shift(1)

    breakout = close > range_high

    df["range_breakout"] = breakout.astype(int)

    return df


# ============================================================
# recent high breakout
# ============================================================

def add_recent_high_breakout(df: pd.DataFrame, window: int = 5):

    df = df.copy()

    high = _safe_numeric(df, "high")
    close = _safe_numeric(df, "close")

    recent_high = high.rolling(window, min_periods=3).max().shift(1)

    breakout = close > recent_high

    df["recent_high_breakout"] = breakout.astype(int)

    return df


# ============================================================
# volatility breakout
# ============================================================

def add_volatility_breakout(df: pd.DataFrame):

    df = df.copy()

    high = _safe_numeric(df, "high")
    low = _safe_numeric(df, "low")
    close = _safe_numeric(df, "close")

    true_range = high - low

    atr = true_range.rolling(14, min_periods=5).mean()

    move = close.diff()

    breakout = move > atr

    df["volatility_breakout"] = breakout.astype(int)

    return df


# ============================================================
# VWAP breakout
# ============================================================

def add_vwap_breakout(df: pd.DataFrame):

    df = df.copy()

    if "vwap" not in df.columns:
        return df

    close = _safe_numeric(df, "close")
    vwap = _safe_numeric(df, "vwap")

    breakout = (close > vwap) & (close.shift(1) <= vwap.shift(1))

    df["vwap_breakout"] = breakout.astype(int)

    return df


# ============================================================
# volume confirmation
# ============================================================

def add_volume_confirmation(df: pd.DataFrame):

    df = df.copy()

    volume = _safe_numeric(df, "volume")

    vol_ma = volume.rolling(20, min_periods=5).mean()

    spike = volume > vol_ma * 1.5

    df["volume_spike"] = spike.astype(int)

    return df


# ============================================================
# fake breakout filter
# ============================================================

def add_fake_breakout_filter(df: pd.DataFrame):

    df = df.copy()

    close = _safe_numeric(df, "close")
    high = _safe_numeric(df, "high")

    rejection = high > high.shift(1)
    rejection &= close < high.shift(1)

    df["fake_breakout"] = rejection.astype(int)

    return df


# ============================================================
# breakout strength
# ============================================================

def add_breakout_strength(df: pd.DataFrame):

    df = df.copy()

    components = []

    for col in [
        "range_breakout",
        "recent_high_breakout",
        "volatility_breakout",
        "vwap_breakout",
        "volume_spike"
    ]:

        if col in df.columns:
            components.append(df[col])

    if components:

        score = sum(components)

        df["breakout_strength"] = score

    else:

        df["breakout_strength"] = 0

    return df


# ============================================================
# full breakout pipeline
# ============================================================

def apply_breakout_signals(df: pd.DataFrame):

    df = df.copy()

    df = add_range_breakout(df)

    df = add_recent_high_breakout(df)

    df = add_volatility_breakout(df)

    df = add_vwap_breakout(df)

    df = add_volume_confirmation(df)

    df = add_fake_breakout_filter(df)

    df = add_breakout_strength(df)

    return df