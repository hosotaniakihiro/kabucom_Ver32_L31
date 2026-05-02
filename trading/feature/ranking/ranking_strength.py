# ============================================================
# File   : trading/features/ranking/ranking_strength.py
# Version: Ver1.0-RANKING-STRENGTH-FEATURE
# ------------------------------------------------------------
# ✔ ranking appearance frequency
# ✔ ranking type diversity
# ✔ ranking position strength
# ✔ ranking persistence
# ✔ ranking velocity
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
# ranking frequency
# ============================================================

def add_ranking_frequency(df: pd.DataFrame):

    df = df.copy()

    if "symbol" not in df.columns:
        return df

    freq = df.groupby("symbol")["symbol"].transform("count")

    df["ranking_frequency"] = freq

    return df


# ============================================================
# ranking type diversity
# ============================================================

def add_ranking_type_diversity(df: pd.DataFrame):

    df = df.copy()

    if "ranking_type" not in df.columns:
        df["ranking_type_diversity"] = 1
        return df

    diversity = df.groupby("symbol")["ranking_type"].transform("nunique")

    df["ranking_type_diversity"] = diversity

    return df


# ============================================================
# ranking position strength
# ============================================================

def add_ranking_position_strength(df: pd.DataFrame):

    df = df.copy()

    if "rank" not in df.columns:
        df["ranking_position_strength"] = 0
        return df

    rank = _safe_numeric(df, "rank")

    strength = (100 - rank).clip(lower=0)

    df["ranking_position_strength"] = strength

    return df


# ============================================================
# ranking persistence
# ============================================================

def add_ranking_persistence(df: pd.DataFrame):

    df = df.copy()

    if "symbol" not in df.columns:
        df["ranking_persistence"] = 0
        return df

    persistence = (
        df.groupby("symbol")
        .cumcount()
    )

    df["ranking_persistence"] = persistence

    return df


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
# ranking strength score
# ============================================================

def add_ranking_strength(df: pd.DataFrame):

    df = df.copy()

    score = 0

    cols = [
        "ranking_frequency",
        "ranking_type_diversity",
        "ranking_position_strength",
        "ranking_persistence"
    ]

    for c in cols:

        if c in df.columns:
            score += df[c]

    df["ranking_strength"] = score

    return df


# ============================================================
# full pipeline
# ============================================================

def apply_ranking_strength_features(df: pd.DataFrame):

    df = df.copy()

    df = add_ranking_frequency(df)

    df = add_ranking_type_diversity(df)

    df = add_ranking_position_strength(df)

    df = add_ranking_persistence(df)

    df = add_ranking_velocity(df)

    df = add_ranking_strength(df)

    return df