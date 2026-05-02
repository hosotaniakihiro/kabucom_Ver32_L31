# ============================================================
# File   : trading/ai/tonosama_detector.py
# Version: Ver1.2-PRODUCTION-TONOSAMA-DETECTOR
# ------------------------------------------------------------
# ✔ 殿様イナゴ検出
# ✔ 急騰天井判定
# ✔ volume climax detection
# ✔ momentum exhaustion
# ✔ ranking pipeline compatible
# ✔ surge / institutional compatible
# ✔ entry guard
# ✔ discord alert integration (NEW)
# ✔ global_data integration
# ✔ numpy vectorized
# ✔ NaN safe
# ✔ production safe
# ============================================================

from __future__ import annotations

import logging
import numpy as np
import pandas as pd

from global_state import global_data
from alerts import notify_tonosama

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

    try:

        m = series.abs().max()

        if m == 0:
            return series

        return series / (m + 1e-6)

    except Exception:

        return series


# ============================================================
# tonosama detection
# ============================================================

def detect_tonosama(df: pd.DataFrame) -> pd.DataFrame:
    """
    殿様イナゴ検出

    入力
    ----
    ranking pipeline dataframe

    出力
    ----
    tonosama_score
    tonosama_probability
    tonosama_flag
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

        volume_speed = _safe(df.get("volume_speed", 0))

        volume_delta = _safe(df.get("volume_delta_1m", 0))

        price_delta = _safe(df.get("price_delta_1m", 0))

        surge_prob = _safe(df.get("surge_probability", 0))

        institutional_prob = _safe(df.get("institutional_probability", 0))

        # ----------------------------------------------------
        # normalize
        # ----------------------------------------------------

        velocity_n = _normalize(velocity)

        accel_n = _normalize(acceleration)

        volume_n = _normalize(volume_speed)

        volume_delta_n = _normalize(volume_delta)

        price_n = _normalize(price_delta)

        # ----------------------------------------------------
        # exhaustion logic
        # ----------------------------------------------------

        momentum_exhaustion = (
            (velocity_n > 0.7)
            & (accel_n < 0.1)
        )

        volume_climax = (
            (volume_n > 0.8)
            & (volume_delta_n > 0.8)
        )

        weak_institution = institutional_prob < 0.4

        # ----------------------------------------------------
        # tonosama score
        # ----------------------------------------------------

        tonosama_score = (
            0.35 * velocity_n
            + 0.25 * volume_n
            + 0.20 * volume_delta_n
            + 0.20 * price_n
        )

        tonosama_score = tonosama_score.clip(0, 1)

        # ----------------------------------------------------
        # probability
        # ----------------------------------------------------

        tonosama_prob = 1 / (1 + np.exp(-6 * (tonosama_score - 0.5)))

        # ----------------------------------------------------
        # final flag
        # ----------------------------------------------------

        tonosama_flag = (
            momentum_exhaustion
            & volume_climax
            & weak_institution
            & (surge_prob > 0.7)
        )

        df["tonosama_score"] = tonosama_score
        df["tonosama_probability"] = tonosama_prob
        df["tonosama_flag"] = tonosama_flag.astype(int)

        # ----------------------------------------------------
        # Discord alert
        # ----------------------------------------------------

        try:

            alert_rows = df[df["tonosama_flag"] == 1]

            if not alert_rows.empty:

                notify_tonosama(alert_rows)

        except Exception:

            logger.exception("[tonosama_alert] failed")

        # ----------------------------------------------------
        # global cache
        # ----------------------------------------------------

        try:

            global_data.tonosama_detector = df

        except Exception:
            pass

        return df

    except Exception:

        logger.exception("[tonosama_detector]")

        return df


# ============================================================
# tonosama flag lookup
# ============================================================

def get_tonosama_flag(symbol: str) -> int:
    """
    symbol単位でflag取得
    """

    try:

        df = getattr(global_data, "tonosama_detector", None)

        if df is None or df.empty:
            return 0

        row = df[df["symbol"] == symbol]

        if row.empty:
            return 0

        return int(row["tonosama_flag"].iloc[0])

    except Exception:

        logger.exception("[get_tonosama_flag]")

        return 0


# ============================================================
# entry guard
# ============================================================

def allow_tonosama_entry(symbol: str) -> bool:
    """
    ENTRY防止ロジック

    True  = entry allowed
    False = entry blocked
    """

    try:

        flag = get_tonosama_flag(symbol)

        if flag == 1:

            logger.info(
                "[TONOSAMA BLOCK] symbol=%s",
                symbol,
            )

            return False

        return True

    except Exception:

        logger.exception("[allow_tonosama_entry]")

        return True