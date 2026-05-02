# ============================================================
# File   : trading/features/ranking/ranking_velocity.py
# Version: Ver1.0-RANKING-VELOCITY-FEATURE
# ------------------------------------------------------------
# ✔ ranking velocity
# ✔ ranking acceleration
# ✔ ranking momentum
# ✔ ranking breakout detection
# ✔ ranking velocity strength
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
# ranking velocity
# ============================================================

def add_ranking_velocity(df: pd.DataFrame):

    df = df.copy()

    if "rank" not in df.columns:
        df["ranking_velocity"] = 0
        return df

    rank = _safe_numeric(df, "rank")

    velocity = -rank.diff()

    df["ranking_velocity"] = velocity

    return df


# ============================================================
# ranking acceleration
# ============================================================

def add_ranking_acceleration(df: pd.DataFrame):

    df = df.copy()

    if "ranking_velocity" not in df.columns:
        df = add_ranking_velocity(df)

    vel = _safe_numeric(df, "ranking_velocity")

    acceleration = vel.diff()

    df["ranking_acceleration"] = acceleration

    return df


# ============================================================
# ranking momentum
# ============================================================

def add_ranking_momentum(df: pd.DataFrame):

    df = df.copy()

    if "ranking_velocity" not in df.columns:
        df = add_ranking_velocity(df)

    vel = _safe_numeric(df, "ranking_velocity")

    momentum = vel.rolling(5, min_periods=1).mean()

    df["ranking_momentum"] = momentum

    return df


# ============================================================
# ranking breakout
# ============================================================

def add_ranking_breakout(df: pd.DataFrame):

    df = df.copy()

    if "rank" not in df.columns:
        df["ranking_breakout"] = 0
        return df

    rank = _safe_numeric(df, "rank")

    prev_best = rank.rolling(10, min_periods=3).min().shift(1)

    breakout = rank < prev_best

    df["ranking_breakout"] = breakout.astype(int)

    return df


# ============================================================
# ranking surge detection
# ============================================================

def add_ranking_surge(df: pd.DataFrame):

    df = df.copy()

    if "ranking_velocity" not in df.columns:
        df = add_ranking_velocity(df)

    vel = _safe_numeric(df, "ranking_velocity")

    vel_std = vel.rolling(20, min_periods=5).std()

    surge = vel > vel_std * 2

    df["ranking_surge"] = surge.astype(int)

    return df


# ============================================================
# ranking velocity strength
# ============================================================

def add_ranking_velocity_strength(df: pd.DataFrame):

    df = df.copy()

    score = 0

    cols = [
        "ranking_velocity",
        "ranking_momentum",
        "ranking_breakout",
        "ranking_surge"
    ]

    for c in cols:

        if c in df.columns:
            score += df[c].fillna(0)

    df["ranking_velocity_strength"] = score

    return df


# ============================================================
# full pipeline
# ============================================================

def apply_ranking_velocity_features(df: pd.DataFrame):

    df = df.copy()

    df = add_ranking_velocity(df)

    df = add_ranking_acceleration(df)

    df = add_ranking_momentum(df)

    df = add_ranking_breakout(df)

    df = add_ranking_surge(df)

    df = add_ranking_velocity_strength(df)

    return df