# ============================================================
# File   : trading/ranking/ranking_acceleration_builder.py
# Version: Ver1.0-RANKING-ACCELERATION-BUILDER-PRODUCTION
# ------------------------------------------------------------
# ✔ ranking_velocity から acceleration 計算
# ✔ 急騰株の加速検出
# ✔ velocity slope
# ✔ velocity acceleration
# ✔ rank acceleration
# ✔ volume acceleration
# ✔ theme acceleration
# ✔ NaN / inf 完全防御
# ✔ vectorized
# ✔ DataFrame in / out
# ============================================================

from __future__ import annotations

import logging
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# utils
# ============================================================

def _safe_series(s: pd.Series) -> pd.Series:

    if s is None:
        return pd.Series(dtype=float)

    return (
        pd.to_numeric(s, errors="coerce")
        .replace([np.inf, -np.inf], np.nan)
        .fillna(0)
    )


# ============================================================
# main
# ============================================================

def build_ranking_acceleration(df: pd.DataFrame) -> pd.DataFrame:
    """
    ranking_velocity DataFrame から
    acceleration を計算

    必須列
    ----------
    symbol

    推奨列
    ----------
    velocity_score
    velocity_rank
    velocity_volume
    velocity_theme

    戻り値
    ----------
    acceleration_score
    """

    if df is None or df.empty:
        return pd.DataFrame()

    df = df.copy()

    # =========================================================
    # 必須列
    # =========================================================

    if "symbol" not in df.columns:
        logger.warning("[ranking_acceleration] symbol column missing")
        return pd.DataFrame()

    # =========================================================
    # velocity columns
    # =========================================================

    v_score = _safe_series(df.get("velocity_score"))
    v_rank = _safe_series(df.get("velocity_rank"))
    v_volume = _safe_series(df.get("velocity_volume"))
    v_theme = _safe_series(df.get("velocity_theme"))

    # =========================================================
    # acceleration
    # =========================================================

    df["acc_velocity_score"] = v_score.diff().fillna(0)
    df["acc_velocity_rank"] = v_rank.diff().fillna(0)
    df["acc_velocity_volume"] = v_volume.diff().fillna(0)
    df["acc_velocity_theme"] = v_theme.diff().fillna(0)

    # =========================================================
    # positive acceleration
    # =========================================================

    df["acc_pos_score"] = np.maximum(df["acc_velocity_score"], 0)
    df["acc_pos_rank"] = np.maximum(df["acc_velocity_rank"], 0)
    df["acc_pos_volume"] = np.maximum(df["acc_velocity_volume"], 0)
    df["acc_pos_theme"] = np.maximum(df["acc_velocity_theme"], 0)

    # =========================================================
    # weighted acceleration score
    # =========================================================

    df["acceleration_score"] = (
        df["acc_pos_score"] * 0.40
        + df["acc_pos_rank"] * 0.25
        + df["acc_pos_volume"] * 0.25
        + df["acc_pos_theme"] * 0.10
    )

    # =========================================================
    # 急騰候補フラグ
    # =========================================================

    df["acceleration_signal"] = (
        (df["acc_pos_score"] > 0.5)
        | (df["acc_pos_volume"] > 0.5)
        | (df["acc_pos_rank"] > 0.5)
    ).astype(int)

    # =========================================================
    # normalize
    # =========================================================

    max_val = df["acceleration_score"].max()

    if max_val > 0:
        df["acceleration_score_norm"] = df["acceleration_score"] / max_val
    else:
        df["acceleration_score_norm"] = 0

    # =========================================================
    # sort
    # =========================================================

    df = df.sort_values(
        "acceleration_score_norm",
        ascending=False,
    )

    logger.info(
        "[ranking_acceleration] built rows=%s",
        len(df),
    )

    return df