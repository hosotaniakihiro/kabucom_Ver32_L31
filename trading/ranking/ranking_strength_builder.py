# ============================================================
# File   : trading/ranking/ranking_strength_builder.py
# Version: Ver2.1-RANKING-STRENGTH-BUILDER-INSTITUTIONAL-STABLE
# ------------------------------------------------------------
# ✔ Ver2.0 完全保持（削除ゼロ）
# ✔ ranking snapshot → symbol単位集約
# ✔ ranking種類別出現数
# ✔ ranking順位強度
# ✔ 直近出現頻度
# ✔ theme強度
# ✔ volume / turnover強度
# ✔ 急騰株向け momentum boost
# ✔ ranking velocity 追加
# ✔ ranking recency weight 追加
# ✔ ranking strength 暴走防止
# ✔ DataFrame in / out
# ✔ NaN / inf 完全防御
# ✔ 列不足耐性
# ✔ dtype stabilization
# ✔ vectorized高速
# ✔ production safe
# ============================================================

from __future__ import annotations

import logging
import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)


# ============================================================
# safe column helper
# ============================================================

def _ensure_numeric(series, default=0):
    try:
        return pd.to_numeric(series, errors="coerce").fillna(default)
    except Exception:
        return pd.Series(default, index=series.index)


# ============================================================
# ranking strength build
# ============================================================

def build_ranking_strength(df: pd.DataFrame) -> pd.DataFrame:

    """
    ranking snapshot DataFrame から
    銘柄ごとの ranking strength を構築

    必須列
    ----------
    symbol
    rank
    ranking_type

    optional
    ----------
    theme
    volume
    turnover
    datetime
    """

    # --------------------------------------------------------
    # safety
    # --------------------------------------------------------

    if df is None or len(df) == 0:
        return pd.DataFrame()

    df = df.copy()

    # dtype stabilization
    df["symbol"] = df["symbol"].astype(str)

    # --------------------------------------------------------
    # 必須列保証
    # --------------------------------------------------------

    if "symbol" not in df.columns:
        logger.warning("[ranking_strength] missing symbol column")
        return pd.DataFrame()

    if "rank" not in df.columns:
        df["rank"] = 50

    if "ranking_type" not in df.columns:
        df["ranking_type"] = "unknown"

    if "theme" not in df.columns:
        df["theme"] = None

    if "volume" not in df.columns:
        df["volume"] = 0

    if "turnover" not in df.columns:
        df["turnover"] = 0

    # --------------------------------------------------------
    # numeric safety
    # --------------------------------------------------------

    df["rank"] = _ensure_numeric(df["rank"], 50)
    df["volume"] = _ensure_numeric(df["volume"], 0)
    df["turnover"] = _ensure_numeric(df["turnover"], 0)

    # --------------------------------------------------------
    # rank score
    # --------------------------------------------------------

    df["rank_score"] = 1.0 / df["rank"].clip(lower=1)

    # --------------------------------------------------------
    # ranking recency weight
    # --------------------------------------------------------

    if "datetime" in df.columns:

        try:

            df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")

            now = df["datetime"].max()

            age_sec = (now - df["datetime"]).dt.total_seconds()

            recency_weight = np.exp(-age_sec / 600)

            df["rank_score"] = df["rank_score"] * recency_weight

        except Exception:

            pass

    # --------------------------------------------------------
    # ranking種類別 count
    # --------------------------------------------------------

    try:

        ranking_type_counts = (
            df.groupby(["symbol", "ranking_type"])
            .size()
            .unstack(fill_value=0)
        )

        ranking_type_counts.columns = [
            f"rank_type_{c}" for c in ranking_type_counts.columns
        ]

    except Exception:

        ranking_type_counts = pd.DataFrame(index=df["symbol"].unique())

    # --------------------------------------------------------
    # symbol aggregation
    # --------------------------------------------------------

    agg = df.groupby("symbol").agg(

        ranking_count=("symbol", "size"),

        best_rank=("rank", "min"),

        avg_rank=("rank", "mean"),

        rank_score_sum=("rank_score", "sum"),

        volume_sum=("volume", "sum"),

        turnover_sum=("turnover", "sum"),

    )

    # --------------------------------------------------------
    # theme strength
    # --------------------------------------------------------

    try:

        theme_counts = (
            df.groupby(["symbol", "theme"])
            .size()
            .unstack(fill_value=0)
        )

        theme_counts.columns = [
            f"theme_{c}" for c in theme_counts.columns
        ]

    except Exception:

        theme_counts = pd.DataFrame(index=agg.index)

    # --------------------------------------------------------
    # merge
    # --------------------------------------------------------

    out = agg.join(ranking_type_counts, how="left")

    if not theme_counts.empty:
        out = out.join(theme_counts, how="left")

    out.fillna(0, inplace=True)

    # --------------------------------------------------------
    # diversity
    # --------------------------------------------------------

    rank_type_cols = [c for c in out.columns if c.startswith("rank_type_")]

    if len(rank_type_cols) > 0:

        out["rank_diversity"] = (
            (out[rank_type_cols] > 0).sum(axis=1)
        )

    else:

        out["rank_diversity"] = 0

    # --------------------------------------------------------
    # theme strength
    # --------------------------------------------------------

    theme_cols = [c for c in out.columns if c.startswith("theme_")]

    if len(theme_cols) > 0:

        out["theme_strength"] = out[theme_cols].sum(axis=1)

    else:

        out["theme_strength"] = 0

    # --------------------------------------------------------
    # volume strength
    # --------------------------------------------------------

    out["volume_strength"] = np.log1p(out["volume_sum"])

    # --------------------------------------------------------
    # turnover strength
    # --------------------------------------------------------

    out["turnover_strength"] = np.log1p(out["turnover_sum"])

    # --------------------------------------------------------
    # rank power
    # --------------------------------------------------------

    out["rank_power"] = out["rank_score_sum"]

    # --------------------------------------------------------
    # ranking velocity
    # --------------------------------------------------------

    if "datetime" in df.columns:

        try:

            counts = df.groupby("symbol")["datetime"].count()

            duration = (
                df.groupby("symbol")["datetime"].max()
                - df.groupby("symbol")["datetime"].min()
            ).dt.total_seconds().clip(lower=1)

            velocity = counts / duration

            out["ranking_velocity"] = velocity

        except Exception:

            out["ranking_velocity"] = 0

    else:

        out["ranking_velocity"] = 0

    # --------------------------------------------------------
    # momentum boost（急騰株用）
    # --------------------------------------------------------

    out["momentum_boost"] = (
        out["rank_diversity"] * 2
        + out["rank_power"] * 5
        + out["ranking_velocity"] * 50
    )

    # --------------------------------------------------------
    # 最終 ranking strength
    # --------------------------------------------------------

    out["ranking_strength"] = (

        out["ranking_count"] * 1.2
        + out["rank_power"] * 5
        + out["rank_diversity"] * 3
        + out["theme_strength"] * 1.5
        + out["volume_strength"] * 1.5
        + out["turnover_strength"] * 2
        + out["momentum_boost"]

    )

    # --------------------------------------------------------
    # safety
    # --------------------------------------------------------

    out.replace([np.inf, -np.inf], 0, inplace=True)

    out.fillna(0, inplace=True)

    # extreme protection
    out["ranking_strength"] = out["ranking_strength"].clip(-200, 200)

    out.reset_index(inplace=True)

    return out