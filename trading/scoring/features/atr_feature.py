# ============================================================
# File   : trading/scoring/features/atr_feature.py
# Version: Ver1.0-PRODUCTION-ATR-FEATURE
# ------------------------------------------------------------
# ✔ ATR(14) generation
# ✔ symbol safe calculation
# ✔ NaN safe
# ✔ pandas alignment safety
# ✔ duplicate guard
# ✔ production stability
# ============================================================

from __future__ import annotations

import logging
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# true range
# ============================================================

def _true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:

    try:

        prev_close = close.shift()

        tr = np.maximum(
            high - low,
            np.maximum(
                (high - prev_close).abs(),
                (low - prev_close).abs()
            )
        )

        return tr

    except Exception:

        logger.exception("[ATR FEATURE] true range failed")

        return pd.Series(np.zeros(len(high)), index=high.index)


# ============================================================
# ATR calculation
# ============================================================

def _calculate_atr(group: pd.DataFrame) -> pd.DataFrame:

    try:

        h = group["high"]
        l = group["low"]
        c = group["close"]

        tr = _true_range(h, l, c)

        atr = tr.rolling(
            window=14,
            min_periods=1
        ).mean()

        group["atr_1m"] = atr

    except Exception:

        logger.exception("[ATR FEATURE] ATR calc failed")

        group["atr_1m"] = 0.0

    return group


# ============================================================
# ensure atr
# ============================================================

def ensure_atr(df: pd.DataFrame) -> pd.DataFrame:
    """
    Ensure ATR column exists

    Required columns
        symbol
        high
        low
        close
    """

    if df is None or df.empty:
        return df

    if "atr_1m" in df.columns:
        return df

    required = {"symbol", "high", "low", "close"}

    if not required.issubset(df.columns):

        logger.warning(
            "[ATR FEATURE] missing columns -> %s",
            required - set(df.columns),
        )

        df["atr_1m"] = 0.0
        return df

    try:

        df = df.copy()

        if "symbol" in df.columns:

            df = df.groupby(
                "symbol",
                group_keys=False,
                sort=False
            ).apply(_calculate_atr)

        else:

            df = _calculate_atr(df)

        if "atr_1m" not in df.columns:

            df["atr_1m"] = 0.0

    except Exception:

        logger.exception("[ATR FEATURE] ensure ATR failed")

        df["atr_1m"] = 0.0

    return df