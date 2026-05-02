# ============================================================
# File   : trading/ranking/ranking_theme_heat_builder.py
# Version: Ver1.0-THEME-HEAT-BUILDER-PRODUCTION
# ------------------------------------------------------------
# ✔ ranking_acceleration から theme heat 構築
# ✔ 同テーマランキング密度
# ✔ 同テーマ acceleration
# ✔ テーマ資金流入検出
# ✔ 急騰テーマ検出
# ✔ vectorized
# ✔ NaN / inf 防御
# ============================================================

from __future__ import annotations

import logging
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# utils
# ============================================================

def _safe(s):

    if s is None:
        return pd.Series(dtype=float)

    return (
        pd.to_numeric(s, errors="coerce")
        .replace([np.inf, -np.inf], np.nan)
        .fillna(0)
    )


# ============================================================
# theme heat builder
# ============================================================

def build_ranking_theme_heat(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return pd.DataFrame()

    if "theme" not in df.columns:
        logger.warning("[theme_heat] theme column missing")
        return pd.DataFrame()

    df = df.copy()

    # =========================================================
    # acceleration
    # =========================================================

    acc = _safe(df.get("acceleration_score_norm"))

    df["theme_acc"] = acc

    # =========================================================
    # theme grouping
    # =========================================================

    g = df.groupby("theme")

    theme_count = g["symbol"].transform("count")
    theme_acc_sum = g["theme_acc"].transform("sum")
    theme_acc_mean = g["theme_acc"].transform("mean")

    df["theme_symbol_count"] = theme_count
    df["theme_acc_sum"] = theme_acc_sum
    df["theme_acc_mean"] = theme_acc_mean

    # =========================================================
    # theme heat score
    # =========================================================

    df["theme_heat_score"] = (
        theme_count * 0.4 +
        theme_acc_sum * 0.4 +
        theme_acc_mean * 0.2
    )

    # =========================================================
    # normalize
    # =========================================================

    m = df["theme_heat_score"].max()

    if m > 0:
        df["theme_heat_norm"] = df["theme_heat_score"] / m
    else:
        df["theme_heat_norm"] = 0

    # =========================================================
    # theme signal
    # =========================================================

    df["theme_heat_signal"] = (
        (df["theme_symbol_count"] >= 2)
        &
        (df["theme_heat_norm"] > 0.3)
    ).astype(int)

    # =========================================================
    # sort
    # =========================================================

    df = df.sort_values(
        "theme_heat_norm",
        ascending=False
    )

    logger.info(
        "[theme_heat] built rows=%s themes=%s",
        len(df),
        df["theme"].nunique()
    )

    return df