# ============================================================
# File   : ats/ats_ranking/filters.py
# Version: Ver1.0-ATS-RANKING-FILTERS
# ============================================================

from __future__ import annotations

import logging
import pandas as pd

from .constants import (
    MIN_ABS_TRADING_VOLUME,
    MIN_VOLUME_SPEED,
    MIN_PRICE,
    MAX_PRICE,
)
from .normalizer import _safe_numeric_series

logger = logging.getLogger(__name__)


def _apply_hard_liquidity_filter(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    try:
        x = df.copy()

        if "current_price" not in x.columns and "price" in x.columns:
            x["current_price"] = x["price"]
        if "trading_volume" not in x.columns and "volume" in x.columns:
            x["trading_volume"] = x["volume"]
        if "volume_speed" not in x.columns and "volume_spike" in x.columns:
            x["volume_speed"] = x["volume_spike"]

        if "current_price" not in x.columns:
            x["current_price"] = 0
        if "trading_volume" not in x.columns:
            x["trading_volume"] = 0
        if "volume_speed" not in x.columns:
            x["volume_speed"] = 0

        x["current_price"] = _safe_numeric_series(x["current_price"], default=0)
        x["trading_volume"] = _safe_numeric_series(x["trading_volume"], default=0)
        x["volume_speed"] = _safe_numeric_series(x["volume_speed"], default=0)

        """logger.info(
            "[ATS RANKING] hard liquidity profile rows=%d price>0=%d vol>=100=%d vspd>0=%d",
            len(x),
            int((x["current_price"] > 0).sum()),
            int((x["trading_volume"] >= MIN_ABS_TRADING_VOLUME).sum()),
            int((x["volume_speed"] > 0).sum()),
        )"""

        before = len(x)

        x = x[
            (x["trading_volume"] >= MIN_ABS_TRADING_VOLUME)
            & (x["current_price"] >= MIN_PRICE)
            & (x["current_price"] <= MAX_PRICE)
        ].copy()

        if "volume_speed" in x.columns and (x["volume_speed"] > 0).any():
            x = x[
                (x["volume_speed"] >= MIN_VOLUME_SPEED)
                | (x["trading_volume"] >= MIN_ABS_TRADING_VOLUME * 3)
            ].copy()

        removed = before - len(x)
        if removed > 0:
            logger.info(
                "[ATS RANKING] hard liquidity removed=%d remain=%d",
                removed,
                len(x),
            )

        return x

    except Exception:
        logger.exception("hard liquidity filter failed")
        return df.copy()