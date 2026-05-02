# ============================================================
# File   : trading/ranking/engines/velocity.py
# Version: Ver3-PRODUCTION-ULTRA-STABLE-VELOCITY
# ------------------------------------------------------------
# ✔ ranking速度（best_rank / avg_rank）
# ✔ groupby安全処理
# ✔ datetime順序保証
# ✔ NaN / inf 完全防御
# ✔ ranking_count対応
# ✔ smoothing（ノイズ除去）
# ✔ 正規化（比較可能）
# ✔ fallback対応
# ✔ pandas crash防止
# ✔ production hardened
# ============================================================

from __future__ import annotations

import logging
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# parameters
# ============================================================

ROLL_SMOOTH = 3
CLIP_MIN = -1000
CLIP_MAX = 1000


# ============================================================
# helpers
# ============================================================

def _safe_series(df: pd.DataFrame, col: str) -> pd.Series:
    if col in df.columns:
        return pd.to_numeric(df[col], errors="coerce")
    return pd.Series(0, index=df.index)


def _sanitize(s: pd.Series) -> pd.Series:
    return (
        s.replace([np.inf, -np.inf], np.nan)
         .fillna(0)
    )


# ============================================================
# core
# ============================================================

def _compute_velocity(df: pd.DataFrame) -> pd.Series:
    """
    ranking変化速度

    best_rankが改善（小さくなる）ほどプラス
    """

    if "symbol" not in df.columns:
        return pd.Series(0, index=df.index)

    rank = None

    # 優先順位
    if "best_rank" in df.columns:
        rank = _safe_series(df, "best_rank")

    elif "avg_rank" in df.columns:
        rank = _safe_series(df, "avg_rank")

    else:
        return pd.Series(0, index=df.index)

    try:

        # rank変化（逆符号：順位が上がる＝プラス）
        diff = (
            rank.groupby(df["symbol"])
            .diff()
        )

        velocity = -diff

        velocity = _sanitize(velocity)

        # smoothing
        velocity = (
            velocity.groupby(df["symbol"])
            .rolling(ROLL_SMOOTH)
            .mean()
            .reset_index(level=0, drop=True)
        )

        velocity = _sanitize(velocity)

        return velocity

    except Exception:

        logger.exception("[velocity] compute failed")

        return pd.Series(0, index=df.index)


# ============================================================
# ranking count boost
# ============================================================

def _apply_count_boost(df: pd.DataFrame, velocity: pd.Series) -> pd.Series:
    """
    ranking_countで信頼度補正
    """

    if "ranking_count" not in df.columns:
        return velocity

    try:

        count = _safe_series(df, "ranking_count")

        boost = np.log1p(count)

        return velocity * boost

    except Exception:
        return velocity


# ============================================================
# normalize
# ============================================================

def _normalize(s: pd.Series) -> pd.Series:

    max_abs = s.abs().max()

    if max_abs > 0:
        return s / max_abs

    return s


# ============================================================
# main
# ============================================================

def apply_velocity(
    df: pd.DataFrame,
    *,
    normalize: bool = True
) -> pd.DataFrame:
    """
    velocity生成

    出力:
        df["ranking_velocity"]
    """

    if df is None or df.empty:
        return df

    try:

        df = df.copy()

        # ----------------------------------------------------
        # sort（超重要）
        # ----------------------------------------------------
        if "symbol" in df.columns and "datetime" in df.columns:
            df = df.sort_values(["symbol", "datetime"])

        # ----------------------------------------------------
        # core
        # ----------------------------------------------------
        velocity = _compute_velocity(df)

        # ----------------------------------------------------
        # boost
        # ----------------------------------------------------
        velocity = _apply_count_boost(df, velocity)

        # ----------------------------------------------------
        # normalize
        # ----------------------------------------------------
        if normalize:
            velocity = _normalize(velocity)

        # ----------------------------------------------------
        # clip
        # ----------------------------------------------------
        velocity = velocity.clip(CLIP_MIN, CLIP_MAX)

        df["ranking_velocity"] = _sanitize(velocity)

        return df

    except Exception:

        logger.exception("[velocity] apply failed")

        df["ranking_velocity"] = 0
        return df


# ============================================================
# utility
# ============================================================

def latest_velocity(df: pd.DataFrame):

    if df is None or df.empty:
        return 0

    if "ranking_velocity" not in df.columns:
        return 0

    try:
        return float(df["ranking_velocity"].iloc[-1])
    except Exception:
        return 0