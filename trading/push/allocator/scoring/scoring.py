# ============================================================
# File   : trading/push/allocator/scoring.py
# Version: Ver1.0-PRODUCTION-PUSH-ALLOCATOR-SCORING
# ------------------------------------------------------------
# ✔ priority scoring engine
# ✔ ranking score normalization
# ✔ hysteresis support
# ✔ state integration
# ✔ NaN / inf guard
# ✔ vectorized processing
# ✔ production safe
# ============================================================

from __future__ import annotations

import logging
import numpy as np
import pandas as pd

from trading.push.allocator.state.state import AllocatorState

logger = logging.getLogger(__name__)


# ============================================================
# normalize ranking score
# ============================================================

def normalize_rank_score(df: pd.DataFrame) -> pd.Series:
    """
    ranking score を 0〜1 に正規化
    """

    if "rank_score" not in df.columns:
        return pd.Series(0.0, index=df.index)

    s = df["rank_score"].astype(float)

    s = s.replace([np.inf, -np.inf], np.nan).fillna(0)

    if s.max() == s.min():
        return pd.Series(0.0, index=df.index)

    return (s - s.min()) / (s.max() - s.min())


# ============================================================
# hysteresis boost
# ============================================================

def apply_hysteresis(
    df: pd.DataFrame,
    state: AllocatorState,
    margin: float,
) -> pd.DataFrame:
    """
    churn防止

    既存 push 銘柄は少し優遇
    """

    if df.empty:
        return df

    boost = []

    for symbol in df["symbol"]:

        if state.contains(symbol):
            boost.append(margin)
        else:
            boost.append(0.0)

    df["hysteresis_boost"] = boost

    return df


# ============================================================
# final scoring
# ============================================================

def apply_scoring(
    df: pd.DataFrame,
    config,
    state: AllocatorState | None = None,
) -> pd.DataFrame:
    """
    allocator最終スコア計算
    """

    if df is None or df.empty:
        return df

    df = df.copy()

    # --------------------------------------------------------
    # normalize ranking score
    # --------------------------------------------------------

    norm_rank = normalize_rank_score(df)

    # --------------------------------------------------------
    # base priority
    # --------------------------------------------------------

    base_priority = df["priority"].astype(float)

    base_priority = base_priority.replace(
        [np.inf, -np.inf],
        np.nan,
    ).fillna(0)

    # --------------------------------------------------------
    # ranking influence
    # --------------------------------------------------------

    rank_component = norm_rank * config.rank_score_multiplier

    # --------------------------------------------------------
    # hysteresis
    # --------------------------------------------------------

    if state is not None:

        df = apply_hysteresis(
            df,
            state,
            config.hysteresis_margin,
        )

        hysteresis = df["hysteresis_boost"]

    else:

        hysteresis = 0.0

    # --------------------------------------------------------
    # final score
    # --------------------------------------------------------

    df["final_score"] = (
        base_priority
        + rank_component
        + hysteresis
    )

    df["final_score"] = df["final_score"].replace(
        [np.inf, -np.inf],
        np.nan,
    ).fillna(0)

    # --------------------------------------------------------
    # sort
    # --------------------------------------------------------

    df = df.sort_values(
        "final_score",
        ascending=False,
        ignore_index=True,
    )

    logger.info(
        "[allocator scoring] "
        f"symbols={len(df)} "
        f"top_score={df['final_score'].iloc[0] if len(df) else 0}"
    )

    return df