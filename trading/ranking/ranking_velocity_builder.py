# ============================================================
# File   : trading/ranking/ranking_velocity_builder.py
# Version: Ver1.0-RANKING-VELOCITY-BUILDER-PRODUCTION
# ------------------------------------------------------------
# ✔ ranking snapshot → symbol単位集約
# ✔ rank velocity（順位加速）
# ✔ rank improvement（順位改善）
# ✔ ranking出現頻度
# ✔ ranking滞在時間
# ✔ rank volatility
# ✔ momentum score（急騰株用）
# ✔ DataFrame in / out
# ✔ NaN / inf 完全防御
# ✔ vectorized高速
# ============================================================

from __future__ import annotations

import logging
import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)


# ============================================================
# safe numeric
# ============================================================

def _ensure_numeric(series, default=0):
    try:
        return pd.to_numeric(series, errors="coerce").fillna(default)
    except Exception:
        return pd.Series(default, index=series.index)


# ============================================================
# ranking velocity builder
# ============================================================

def build_ranking_velocity(df: pd.DataFrame) -> pd.DataFrame:
    """
    ranking snapshot から
    ランキング加速（velocity）を算出

    必須列
    ----------
    symbol
    rank

    optional
    ----------
    datetime
    ranking_type
    """

    # --------------------------------------------------------
    # safety
    # --------------------------------------------------------

    if df is None or len(df) == 0:
        return pd.DataFrame()

    df = df.copy()

    if "symbol" not in df.columns:
        logger.warning("[ranking_velocity] symbol column missing")
        return pd.DataFrame()

    if "rank" not in df.columns:
        df["rank"] = 50

    if "datetime" not in df.columns:
        df["datetime"] = pd.Timestamp.now()

    # --------------------------------------------------------
    # numeric safety
    # --------------------------------------------------------

    df["rank"] = _ensure_numeric(df["rank"], 50)

    # --------------------------------------------------------
    # sort
    # --------------------------------------------------------

    df.sort_values(["symbol", "datetime"], inplace=True)

    # --------------------------------------------------------
    # rank diff
    # --------------------------------------------------------

    df["rank_diff"] = df.groupby("symbol")["rank"].diff()

    # rank improvement
    df["rank_improve"] = -df["rank_diff"]

    # --------------------------------------------------------
    # velocity
    # --------------------------------------------------------

    df["rank_velocity"] = df["rank_improve"]

    # --------------------------------------------------------
    # volatility
    # --------------------------------------------------------

    rank_volatility = (
        df.groupby("symbol")["rank_diff"]
        .std()
        .fillna(0)
    )

    # --------------------------------------------------------
    # improvement total
    # --------------------------------------------------------

    rank_improve_sum = (
        df.groupby("symbol")["rank_improve"]
        .sum()
        .fillna(0)
    )

    # --------------------------------------------------------
    # average velocity
    # --------------------------------------------------------

    rank_velocity_mean = (
        df.groupby("symbol")["rank_velocity"]
        .mean()
        .fillna(0)
    )

    # --------------------------------------------------------
    # best rank improvement
    # --------------------------------------------------------

    best_improve = (
        df.groupby("symbol")["rank_improve"]
        .max()
        .fillna(0)
    )

    # --------------------------------------------------------
    # appearance count
    # --------------------------------------------------------

    appearance = df.groupby("symbol").size()

    # --------------------------------------------------------
    # ranking persistence
    # --------------------------------------------------------

    persistence = appearance

    # --------------------------------------------------------
    # assemble
    # --------------------------------------------------------

    out = pd.DataFrame({

        "rank_velocity_mean": rank_velocity_mean,

        "rank_improve_total": rank_improve_sum,

        "rank_improve_best": best_improve,

        "rank_volatility": rank_volatility,

        "ranking_appearance": appearance,

        "ranking_persistence": persistence,

    })

    # --------------------------------------------------------
    # momentum score
    # --------------------------------------------------------

    out["ranking_momentum"] = (

        out["rank_velocity_mean"] * 8
        + out["rank_improve_total"] * 2
        + out["rank_improve_best"] * 3
        + out["ranking_appearance"] * 1.5

    )

    # --------------------------------------------------------
    # safety
    # --------------------------------------------------------

    out.replace([np.inf, -np.inf], 0, inplace=True)
    out.fillna(0, inplace=True)

    out.reset_index(inplace=True)

    return out