# ============================================================
# File   : trading/ai/institutional_flow_detector.py
# Version: Ver1.0-INSTITUTIONAL-FLOW-DETECTOR
# ------------------------------------------------------------
# ✔ institutional flow detection
# ✔ volume spike detection
# ✔ momentum pressure
# ✔ breakout confirmation
# ✔ ranking pipeline compatible
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
# normalize helper
# ============================================================

def _normalize(series):

    m = series.abs().max()

    if m == 0:
        return series

    return series / (m + 1e-6)


# ============================================================
# institutional flow detection
# ============================================================

def detect_institutional_flow(df: pd.DataFrame) -> pd.DataFrame:
    """
    機関フロー検出

    入力
    ----
    ranking metrics dataframe

    出力
    ----
    institutional_flow_score
    institutional_probability
    """

    if df is None or df.empty:
        return df

    try:

        df = df.copy()

        # ----------------------------------------------------
        # feature extraction
        # ----------------------------------------------------

        volume_speed = _safe(df.get("volume_speed", 0))

        volume_delta = _safe(df.get("volume_delta_1m", 0))

        turnover = _safe(df.get("trading_value", 0))

        velocity = _safe(df.get("velocity_score", 0))

        acceleration = _safe(df.get("acceleration_score", 0))

        breakout = _safe(df.get("breakout_strength", 0))

        price_delta = _safe(df.get("price_delta_1m", 0))

        # ----------------------------------------------------
        # normalize
        # ----------------------------------------------------

        volume_speed_n = _normalize(volume_speed)
        volume_delta_n = _normalize(volume_delta)
        turnover_n = _normalize(turnover)
        velocity_n = _normalize(velocity)
        accel_n = _normalize(acceleration)
        breakout_n = _normalize(breakout)
        price_n = _normalize(price_delta)

        # ----------------------------------------------------
        # institutional flow score
        # ----------------------------------------------------

        flow_score = (
            0.30 * volume_speed_n
            + 0.20 * volume_delta_n
            + 0.20 * turnover_n
            + 0.15 * velocity_n
            + 0.10 * accel_n
            + 0.05 * breakout_n
        )

        flow_score = flow_score.clip(-1, 1)

        # ----------------------------------------------------
        # probability
        # ----------------------------------------------------

        flow_prob = 1 / (1 + np.exp(-5 * flow_score))

        df["institutional_flow_score"] = flow_score

        df["institutional_probability"] = flow_prob

        # ----------------------------------------------------
        # institutional flag
        # ----------------------------------------------------

        df["institutional_flag"] = (
            (flow_prob > 0.75)
            & (price_n > 0)
        ).astype(int)

        return df

    except Exception:

        logger.exception("[institutional_flow_detector]")

        return df