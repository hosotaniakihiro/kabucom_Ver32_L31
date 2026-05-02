# ============================================================
# File   : trading/indicators/support_resistance.py
# Version: Ver1.0-PRODUCTION-SUPPORT-RESISTANCE
# ------------------------------------------------------------
# ✔ support detection
# ✔ resistance detection
# ✔ pivot points
# ✔ breakout detection
# ✔ support bounce
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
# support / resistance calculation
# ============================================================

def calc_support_resistance(
    df: pd.DataFrame,
    window: int = 20
) -> pd.DataFrame:

    df = df.copy()

    high = _safe_numeric(df, "high")
    low = _safe_numeric(df, "low")

    resistance = high.rolling(window, min_periods=5).max()
    support = low.rolling(window, min_periods=5).min()

    df["resistance"] = resistance
    df["support"] = support

    return df


# ============================================================
# pivot point
# ============================================================

def add_pivot_points(df: pd.DataFrame) -> pd.DataFrame:

    df = df.copy()

    high = _safe_numeric(df, "high")
    low = _safe_numeric(df, "low")
    close = _safe_numeric(df, "close")

    pivot = (high + low + close) / 3

    r1 = 2 * pivot - low
    s1 = 2 * pivot - high

    r2 = pivot + (high - low)
    s2 = pivot - (high - low)

    df["pivot"] = pivot
    df["resistance_1"] = r1
    df["support_1"] = s1
    df["resistance_2"] = r2
    df["support_2"] = s2

    return df


# ============================================================
# resistance breakout
# ============================================================

def add_resistance_breakout(df: pd.DataFrame) -> pd.DataFrame:

    df = df.copy()

    if "resistance" not in df.columns:
        return df

    close = _safe_numeric(df, "close")

    resistance = df["resistance"]

    breakout = (close > resistance.shift(1))

    df["resistance_breakout"] = breakout.astype(int)

    return df


# ============================================================
# support breakdown
# ============================================================

def add_support_breakdown(df: pd.DataFrame) -> pd.DataFrame:

    df = df.copy()

    if "support" not in df.columns:
        return df

    close = _safe_numeric(df, "close")

    support = df["support"]

    breakdown = close < support.shift(1)

    df["support_breakdown"] = breakdown.astype(int)

    return df


# ============================================================
# support bounce
# ============================================================

def add_support_bounce(df: pd.DataFrame) -> pd.DataFrame:

    df = df.copy()

    if "support" not in df.columns:
        return df

    low = _safe_numeric(df, "low")
    close = _safe_numeric(df, "close")

    support = df["support"]

    bounce = (low <= support) & (close > support)

    df["support_bounce"] = bounce.astype(int)

    return df


# ============================================================
# resistance rejection
# ============================================================

def add_resistance_rejection(df: pd.DataFrame) -> pd.DataFrame:

    df = df.copy()

    if "resistance" not in df.columns:
        return df

    high = _safe_numeric(df, "high")
    close = _safe_numeric(df, "close")

    resistance = df["resistance"]

    rejection = (high >= resistance) & (close < resistance)

    df["resistance_rejection"] = rejection.astype(int)

    return df


# ============================================================
# strength score
# ============================================================

def add_sr_strength(df: pd.DataFrame) -> pd.DataFrame:

    df = df.copy()

    score_cols = [
        "resistance_breakout",
        "support_bounce",
        "support_breakdown",
    ]

    score = 0

    for c in score_cols:

        if c in df.columns:
            score += df[c]

    df["sr_strength"] = score

    return df


# ============================================================
# full pipeline
# ============================================================

def apply_support_resistance(df: pd.DataFrame) -> pd.DataFrame:

    df = df.copy()

    df = calc_support_resistance(df)

    df = add_pivot_points(df)

    df = add_resistance_breakout(df)

    df = add_support_breakdown(df)

    df = add_support_bounce(df)

    df = add_resistance_rejection(df)

    df = add_sr_strength(df)

    return df