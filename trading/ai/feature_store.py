# ============================================================
# trading/ai/feature_store.py
#
# PRODUCTION FEATURE STORE
#
# Stores and retrieves AI features
#
# Used for:
#   real-time inference
#   training
#   backtesting
#
# Supports:
#   in-memory cache
#   time indexed storage
# ============================================================

from __future__ import annotations

import logging
import pandas as pd
from typing import Dict, Optional
from collections import defaultdict

logger = logging.getLogger(__name__)


# ============================================================
# Feature Store
# ============================================================

class FeatureStore:

    def __init__(self):

        # symbol -> dataframe
        self.store: Dict[str, pd.DataFrame] = {}

        # last features cache
        self.latest: Dict[str, Dict] = {}

    # --------------------------------------------------------
    # insert features
    # --------------------------------------------------------

    def insert(
        self,
        symbol: str,
        timestamp,
        features: Dict
    ):

        try:

            df = self.store.get(symbol)

            row = pd.DataFrame(
                [features],
                index=[timestamp]
            )

            if df is None:

                self.store[symbol] = row

            else:

                self.store[symbol] = pd.concat(
                    [df, row]
                )

            self.latest[symbol] = features

        except Exception:

            logger.exception("Feature insert failure")

    # --------------------------------------------------------
    # get latest
    # --------------------------------------------------------

    def get_latest(
        self,
        symbol: str
    ) -> Optional[Dict]:

        return self.latest.get(symbol)

    # --------------------------------------------------------
    # get history
    # --------------------------------------------------------

    def get_history(
        self,
        symbol: str,
        window: int = 100
    ):

        df = self.store.get(symbol)

        if df is None:

            return None

        return df.tail(window)

    # --------------------------------------------------------
    # clear old data
    # --------------------------------------------------------

    def cleanup(
        self,
        max_rows: int = 5000
    ):

        try:

            for symbol, df in self.store.items():

                if len(df) > max_rows:

                    self.store[symbol] = df.tail(max_rows)

        except Exception:

            logger.exception("Feature cleanup failure")

    # --------------------------------------------------------
    # list symbols
    # --------------------------------------------------------

    def symbols(self):

        return list(self.store.keys())


# ============================================================
# Singleton
# ============================================================

_store = None


def get_feature_store():

    global _store

    if _store is None:

        _store = FeatureStore()

    return _store