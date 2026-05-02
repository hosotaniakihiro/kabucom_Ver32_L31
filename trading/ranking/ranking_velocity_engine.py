# ============================================================
# File   : trading/ranking/ranking_velocity_engine.py
# Version: Ver2.0-PRODUCTION-INSTITUTIONAL-VELOCITY-FULL
# ------------------------------------------------------------
# ✔ Ver1.0 全機能保持（削除ゼロ）
# ✔ ranking appearance frequency
# ✔ ranking velocity（順位変化）
# ✔ rank acceleration
# ✔ theme clustering
# ✔ NEW: price velocity（最重要）
# ✔ NEW: volume velocity
# ✔ NEW: zscore normalization
# ✔ NEW: robust scoring（tanh）
# ✔ NEW: NaN / inf 完全防御
# ✔ production hardened
# ============================================================

from __future__ import annotations

import pandas as pd
import numpy as np


# ============================================================
# safe utils
# ============================================================

def _safe_series(s: pd.Series) -> pd.Series:

    if s is None:
        return pd.Series(dtype=float)

    s = pd.to_numeric(s, errors="coerce")
    s = s.replace([np.inf, -np.inf], np.nan)
    return s.fillna(0.0)


def _zscore(s: pd.Series) -> pd.Series:

    s = _safe_series(s)

    std = s.std()

    if std == 0 or np.isnan(std):
        return pd.Series(0.0, index=s.index)

    return (s - s.mean()) / std


# ============================================================
# appearance frequency
# ============================================================

def _appearance_frequency(df):

    freq = df.groupby("symbol").size()

    df["appearance_freq"] = df["symbol"].map(freq)

    return df


# ============================================================
# ranking velocity（順位ベース）
# ============================================================

def _ranking_velocity(df):

    df = df.sort_values(["symbol", "snapshot_time"])

    df["rank_prev"] = df.groupby("symbol")["rank"].shift(1)

    df["rank_change"] = df["rank_prev"] - df["rank"]

    df["rank_velocity"] = df["rank_change"].fillna(0)

    return df


# ============================================================
# rank acceleration
# ============================================================

def _rank_accel(df):

    df["rank_velocity_prev"] = df.groupby("symbol")["rank_velocity"].shift(1)

    df["rank_accel"] = df["rank_velocity"] - df["rank_velocity_prev"]

    df["rank_accel"] = df["rank_accel"].fillna(0)

    return df


# ============================================================
# theme clustering
# ============================================================

def _theme_strength(df):

    if "theme" not in df.columns:
        df["theme_strength"] = 0.0
        return df

    theme_freq = df.groupby("theme")["symbol"].nunique()

    df["theme_strength"] = df["theme"].map(theme_freq)

    return df


# ============================================================
# NEW: price velocity（最重要）
# ============================================================

def _price_velocity(df):

    if "price" not in df.columns:
        df["price_velocity"] = 0.0
        return df

    df = df.sort_values(["symbol", "snapshot_time"])

    # 短期変化 + 平滑化
    vel = (
        df.groupby("symbol")["price"]
        .pct_change(3)
        .rolling(5)
        .mean()
    )

    df["price_velocity"] = _safe_series(vel)

    return df


# ============================================================
# NEW: volume velocity
# ============================================================

def _volume_velocity(df):

    if "volume" not in df.columns:
        df["volume_velocity"] = 0.0
        return df

    df = df.sort_values(["symbol", "snapshot_time"])

    vel = (
        df.groupby("symbol")["volume"]
        .pct_change(3)
        .rolling(5)
        .mean()
    )

    df["volume_velocity"] = _safe_series(vel)

    return df


# ============================================================
# FINAL SCORE
# ============================================================

def _build_velocity_score(df):

    # 各要素をzscore化
    z_price = _zscore(df.get("price_velocity", 0))
    z_volume = _zscore(df.get("volume_velocity", 0))
    z_rank = _zscore(df.get("rank_velocity", 0))
    z_accel = _zscore(df.get("rank_accel", 0))

    # 重み（チューニング可）
    score = (
        z_price * 0.5
        + z_volume * 0.2
        + z_rank * 0.2
        + z_accel * 0.1
    )

    # 外れ値耐性（重要）
    df["_score_velocity"] = np.tanh(score)

    return df


# ============================================================
# main engine
# ============================================================

def apply_ranking_velocity_engine(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    df = df.copy()

    if "symbol" not in df.columns:
        return df

    # --------------------------------------------------------
    # core features（既存）
    # --------------------------------------------------------

    df = _appearance_frequency(df)

    if "rank" in df.columns:
        df = _ranking_velocity(df)
        df = _rank_accel(df)
    else:
        df["rank_velocity"] = 0.0
        df["rank_accel"] = 0.0

    df = _theme_strength(df)

    # --------------------------------------------------------
    # NEW velocity
    # --------------------------------------------------------

    df = _price_velocity(df)
    df = _volume_velocity(df)

    # --------------------------------------------------------
    # final score
    # --------------------------------------------------------

    df = _build_velocity_score(df)

    return df