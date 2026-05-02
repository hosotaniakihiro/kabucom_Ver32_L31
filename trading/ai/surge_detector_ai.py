# ============================================================
# File   : trading/ai/surge_detector_ai.py
# Version: Ver1.0-SURGE-DETECTOR-AI
# ------------------------------------------------------------
# ✔ 急騰確率スコア
# ✔ ranking pipeline 連携
# ✔ theme momentum
# ✔ volume acceleration
# ✔ breakout strength
# ✔ numpy vectorized
# ✔ NaN safe
# ✔ production safe
# ============================================================

from __future__ import annotations

import logging
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# safe numeric
# ============================================================

def _safe(series):

    return (
        pd.to_numeric(series, errors="coerce")
        .replace([np.inf, -np.inf], np.nan)
        .fillna(0)
    )


# ============================================================
# surge probability
# ============================================================

def build_surge_probability(df: pd.DataFrame) -> pd.DataFrame:
    """
    急騰確率計算

    入力
    ----
    ranking metrics dataframe

    出力
    ----
    surge_probability
    surge_score
    """

    if df is None or df.empty:
        return df

    try:

        df = df.copy()

        # ----------------------------------------------------
        # feature extraction
        # ----------------------------------------------------

        velocity = _safe(df.get("velocity_score", 0))

        acceleration = _safe(df.get("acceleration_score", 0))

        breakout = _safe(df.get("breakout_strength", 0))

        theme = _safe(df.get("theme_heat_score", 0))

        volume = _safe(df.get("volume_speed", 0))

        rank = _safe(df.get("rank_score", 0))

        # ----------------------------------------------------
        # normalization
        # ----------------------------------------------------

        velocity_n = velocity / (velocity.abs().max() + 1e-6)
        accel_n = acceleration / (acceleration.abs().max() + 1e-6)
        breakout_n = breakout / (breakout.abs().max() + 1e-6)
        theme_n = theme / (theme.abs().max() + 1e-6)
        volume_n = volume / (volume.abs().max() + 1e-6)
        rank_n = rank / (rank.abs().max() + 1e-6)

        # ----------------------------------------------------
        # surge score
        # ----------------------------------------------------

        surge_score = (
            0.25 * velocity_n
            + 0.25 * accel_n
            + 0.20 * breakout_n
            + 0.15 * theme_n
            + 0.10 * volume_n
            + 0.05 * rank_n
        )

        surge_score = surge_score.clip(-1, 1)

        # ----------------------------------------------------
        # probability
        # ----------------------------------------------------

        surge_prob = 1 / (1 + np.exp(-4 * surge_score))

        df["surge_score"] = surge_score

        df["surge_probability"] = surge_prob

        return df

    except Exception:

        logger.exception("[surge_detector]")

        return df