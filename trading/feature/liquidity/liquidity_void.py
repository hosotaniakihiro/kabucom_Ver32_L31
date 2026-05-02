# ============================================================
# File   : trading/features/liquidity/liquidity_void.py
# Version: Ver1.0-PRODUCTION-LIQUIDITY-VOID
# ------------------------------------------------------------
# ✔ liquidity void detection
# ✔ price gap detection
# ✔ imbalance detection
# ✔ fast move detection
# ✔ liquidity vacuum strength
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
# price gap detection
# ============================================================

def add_price_gap(df: pd.DataFrame):

    df = df.copy()

    open_ = _safe_numeric(df, "open")
    close = _safe_numeric(df, "close")

    prev_close = close.shift(1)

    gap = (open_ - prev_close) / (prev_close + 1e-10)

    df["price_gap"] = gap

    df["gap_up"] = (gap > 0.01).astype(int)
    df["gap_down"] = (gap < -0.01).astype(int)

    return df


# ============================================================
# fast move detection
# ============================================================

def add_fast_move(df: pd.DataFrame):

    df = df.copy()

    close = _safe_numeric(df, "close")

    move = close.diff()

    move_std = move.rolling(20, min_periods=5).std()

    fast = move > move_std * 2

    df["fast_move"] = fast.astype(int)

    return df


# ============================================================
# imbalance detection
# ============================================================

def add_price_imbalance(df: pd.DataFrame):

    df = df.copy()

    high = _safe_numeric(df, "high")
    low = _safe_numeric(df, "low")

    range_ = high - low

    avg_range = range_.rolling(20, min_periods=5).mean()

    imbalance = range_ > avg_range * 1.5

    df["price_imbalance"] = imbalance.astype(int)

    return df


# ============================================================
# liquidity void detection
# ============================================================

def add_liquidity_void(df: pd.DataFrame):

    df = df.copy()

    if "price_imbalance" not in df.columns:
        df = add_price_imbalance(df)

    imbalance = df["price_imbalance"]

    close = _safe_numeric(df, "close")

    move = close.diff().abs()

    avg_move = move.rolling(20, min_periods=5).mean()

    void = (imbalance == 1) & (move > avg_move * 2)

    df["liquidity_void"] = void.astype(int)

    return df


# ============================================================
# vacuum strength
# ============================================================

def add_liquidity_strength(df: pd.DataFrame):

    df = df.copy()

    score = 0

    cols = [
        "gap_up",
        "fast_move",
        "price_imbalance",
        "liquidity_void"
    ]

    for c in cols:

        if c in df.columns:
            score += df[c]

    df["liquidity_strength"] = score

    return df


# ============================================================
# full pipeline
# ============================================================

def apply_liquidity_void_features(df: pd.DataFrame):

    df = df.copy()

    df = add_price_gap(df)

    df = add_fast_move(df)

    df = add_price_imbalance(df)

    df = add_liquidity_void(df)

    df = add_liquidity_strength(df)

    return df