# ============================================================
# File   : trading/feature/volume/accumulation.py
# Version: Ver1.0-INSTITUTIONAL-ACCUMULATION-DETECTOR
# ------------------------------------------------------------
# ✔ volume accumulation
# ✔ price absorption
# ✔ stealth accumulation
# ✔ VWAP accumulation
# ✔ institutional accumulation score
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
# volume accumulation
# ============================================================

def add_volume_accumulation(df: pd.DataFrame):

    df = df.copy()

    volume = _safe_numeric(df, "volume")

    vol_ma = volume.rolling(20, min_periods=5).mean()

    accumulation = volume > vol_ma * 1.2

    df["volume_accumulation"] = accumulation.astype(int)

    return df


# ============================================================
# price absorption
# ============================================================

def add_price_absorption(df: pd.DataFrame):

    df = df.copy()

    close = _safe_numeric(df, "close")
    volume = _safe_numeric(df, "volume")

    price_change = close.diff()

    vol_ma = volume.rolling(20, min_periods=5).mean()

    absorption = (price_change.abs() < close * 0.002) & (volume > vol_ma)

    df["price_absorption"] = absorption.astype(int)

    return df


# ============================================================
# stealth accumulation
# ============================================================

def add_stealth_accumulation(df: pd.DataFrame):

    df = df.copy()

    close = _safe_numeric(df, "close")

    ma20 = close.rolling(20).mean()

    slow_uptrend = close > ma20

    tight_range = (close.rolling(10).max() - close.rolling(10).min()) / close < 0.01

    stealth = slow_uptrend & tight_range

    df["stealth_accumulation"] = stealth.astype(int)

    return df


# ============================================================
# VWAP accumulation
# ============================================================

def add_vwap_accumulation(df: pd.DataFrame):

    df = df.copy()

    if "vwap" not in df.columns:
        return df

    close = _safe_numeric(df, "close")

    vwap = _safe_numeric(df, "vwap")

    dev = (close - vwap) / (vwap + 1e-10)

    accumulation = (dev > -0.002) & (dev < 0.002)

    df["vwap_accumulation"] = accumulation.astype(int)

    return df


# ============================================================
# accumulation strength
# ============================================================

def add_accumulation_strength(df: pd.DataFrame):

    df = df.copy()

    score = 0

    cols = [
        "volume_accumulation",
        "price_absorption",
        "stealth_accumulation",
        "vwap_accumulation"
    ]

    for c in cols:

        if c in df.columns:
            score += df[c]

    df["accumulation_strength"] = score

    return df


# ============================================================
# full pipeline
# ============================================================

def apply_accumulation_features(df: pd.DataFrame):

    df = df.copy()

    df = add_volume_accumulation(df)

    df = add_price_absorption(df)

    df = add_stealth_accumulation(df)

    df = add_vwap_accumulation(df)

    df = add_accumulation_strength(df)

    return df