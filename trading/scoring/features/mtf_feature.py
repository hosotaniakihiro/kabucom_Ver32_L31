# ============================================================
# File   : trading/scoring/features/mtf_feature.py
# Version: Ver1.0-PRODUCTION-MTF-FEATURE
# ------------------------------------------------------------
# ✔ Multi Timeframe Momentum generation
# ✔ mtf_fast
# ✔ mtf_slow
# ✔ mtf_strength
# ✔ symbol safe calculation
# ✔ pandas alignment safety
# ✔ NaN safe
# ✔ production stability
# ============================================================

from __future__ import annotations

import logging
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# calculate mtf
# ============================================================

def _calculate_mtf(group: pd.DataFrame) -> pd.DataFrame:

    try:

        close = group["close"]

        # short momentum
        mtf_fast = close.diff(3)

        # medium momentum
        mtf_slow = close.diff(10)

        # normalized momentum
        mtf_fast_pct = mtf_fast / (close.shift(3) + 1e-9)
        mtf_slow_pct = mtf_slow / (close.shift(10) + 1e-9)

        # mtf strength
        mtf_strength = (
            0.6 * mtf_fast_pct +
            0.4 * mtf_slow_pct
        )

        group["mtf_fast"] = mtf_fast.fillna(0)

        group["mtf_slow"] = mtf_slow.fillna(0)

        group["mtf_strength"] = (
            mtf_strength
            .replace([np.inf, -np.inf], 0)
            .fillna(0)
        )

        # final mtf
        group["mtf"] = group["mtf_strength"]

    except Exception:

        logger.exception("[MTF FEATURE] mtf calculation failed")

        group["mtf_fast"] = 0.0
        group["mtf_slow"] = 0.0
        group["mtf_strength"] = 0.0
        group["mtf"] = 0.0

    return group


# ============================================================
# ensure mtf
# ============================================================

def ensure_mtf(df: pd.DataFrame) -> pd.DataFrame:
    """
    Multi timeframe momentum generator

    Required column
        close

    Output columns
        mtf_fast
        mtf_slow
        mtf_strength
        mtf
    """

    if df is None or df.empty:
        return df

    if "close" not in df.columns:
        return df

    try:

        df = df.copy()

        if "symbol" in df.columns:

            df = df.groupby(
                "symbol",
                group_keys=False,
                sort=False
            ).apply(_calculate_mtf)

        else:

            df = _calculate_mtf(df)

    except Exception:

        logger.exception("[MTF FEATURE] ensure mtf failed")

        df["mtf_fast"] = 0.0
        df["mtf_slow"] = 0.0
        df["mtf_strength"] = 0.0
        df["mtf"] = 0.0

    return df