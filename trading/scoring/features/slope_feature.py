# ============================================================
# File   : trading/scoring/features/slope_feature.py
# Version: Ver1.0-PRODUCTION-SLOPE-FEATURE
# ------------------------------------------------------------
# ✔ slope generation
# ✔ slope_abs generation
# ✔ slope_pct generation
# ✔ slope_ma smoothing
# ✔ symbol safe calculation
# ✔ pandas alignment safety
# ✔ production stability
# ============================================================

from __future__ import annotations

import logging
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# slope calculation
# ============================================================

def _calculate_slope(group: pd.DataFrame) -> pd.DataFrame:

    try:

        close = group["close"]

        # 基本 slope
        slope = close.diff()

        # slope absolute
        slope_abs = slope.abs()

        # slope percent
        slope_pct = slope / (close.shift() + 1e-9)

        # smoothing
        slope_ma = slope.rolling(
            window=5,
            min_periods=1
        ).mean()

        group["slope"] = slope.fillna(0)

        group["slope_abs"] = slope_abs.fillna(0)

        group["slope_pct"] = slope_pct.replace(
            [np.inf, -np.inf],
            0
        ).fillna(0)

        group["slope_ma"] = slope_ma.fillna(0)

    except Exception:

        logger.exception("[SLOPE FEATURE] slope calculation failed")

        group["slope"] = 0.0
        group["slope_abs"] = 0.0
        group["slope_pct"] = 0.0
        group["slope_ma"] = 0.0

    return group


# ============================================================
# ensure slope
# ============================================================

def ensure_slope(df: pd.DataFrame) -> pd.DataFrame:
    """
    Generate slope based features

    Required column
        close

    Output columns
        slope
        slope_abs
        slope_pct
        slope_ma
    """

    if df is None or df.empty:
        return df

    if "close" not in df.columns:
        return df

    try:

        df = df.copy()

        # symbol単位計算
        if "symbol" in df.columns:

            df = df.groupby(
                "symbol",
                group_keys=False,
                sort=False
            ).apply(_calculate_slope)

        else:

            df = _calculate_slope(df)

    except Exception:

        logger.exception("[SLOPE FEATURE] ensure slope failed")

        df["slope"] = 0.0
        df["slope_abs"] = 0.0
        df["slope_pct"] = 0.0
        df["slope_ma"] = 0.0

    return df