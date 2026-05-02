# ============================================================
# trading/ai/microstructure/iceberg_detector.py
# PRODUCTION ICEBERG DETECTOR
#
# Detects hidden iceberg orders using:
#   repeated trade sizes
#   refill pattern
#   price stagnation with heavy execution
#   absorption behaviour
#
# Designed for real-time microstructure analysis
# ============================================================

from __future__ import annotations

import numpy as np
import pandas as pd
import logging
from typing import Dict

logger = logging.getLogger(__name__)


# ============================================================
# Utility
# ============================================================

def _safe_div(a, b):
    if b == 0:
        return 0.0
    return float(a) / float(b)


# ============================================================
# Iceberg Detector
# ============================================================

class IcebergDetector:

    def __init__(self):

        # same size repeat threshold
        self.repeat_threshold = 6

        # absorption threshold
        self.absorption_volume_threshold = 20000

        # price stagnation threshold
        self.price_move_threshold = 0.001

        # refill detection window
        self.refill_window = 12

    # --------------------------------------------------------
    # Public detect
    # --------------------------------------------------------

    def detect(
        self,
        trades: pd.DataFrame,
        bid_volume: float,
        ask_volume: float,
        last_price: float
    ) -> float:

        if trades is None or len(trades) < 10:
            return 0.0

        try:

            repeat_score = self._repeat_trade_score(trades)

            absorption_score = self._absorption_score(
                trades,
                bid_volume,
                ask_volume
            )

            refill_score = self._refill_score(trades)

            stagnation_score = self._price_stagnation_score(
                trades,
                last_price
            )

            score = (
                repeat_score * 0.35
                + absorption_score * 0.30
                + refill_score * 0.20
                + stagnation_score * 0.15
            )

            return float(min(score, 1.0))

        except Exception:

            logger.exception("IcebergDetector failure")

            return 0.0

    # --------------------------------------------------------
    # Repeating trade sizes
    # --------------------------------------------------------

    def _repeat_trade_score(self, trades: pd.DataFrame) -> float:

        sizes = trades["size"].values

        if len(sizes) < 5:
            return 0.0

        rounded = np.round(sizes)

        unique, counts = np.unique(rounded, return_counts=True)

        max_repeat = counts.max()

        score = min(
            max_repeat / self.repeat_threshold,
            1.0
        )

        return float(score)

    # --------------------------------------------------------
    # Absorption detection
    # --------------------------------------------------------

    def _absorption_score(
        self,
        trades: pd.DataFrame,
        bid_volume: float,
        ask_volume: float
    ) -> float:

        total_trade = trades["size"].sum()

        book_liquidity = bid_volume + ask_volume

        if book_liquidity == 0:
            return 0.0

        ratio = total_trade / book_liquidity

        if total_trade > self.absorption_volume_threshold:
            ratio *= 1.2

        return float(min(ratio, 1.0))

    # --------------------------------------------------------
    # Refill detection
    # --------------------------------------------------------

    def _refill_score(self, trades: pd.DataFrame) -> float:

        if len(trades) < self.refill_window:
            return 0.0

        recent = trades.tail(self.refill_window)

        prices = recent["price"].values

        price_std = np.std(prices)

        if price_std < 0.01:

            sizes = recent["size"].values

            mean = np.mean(sizes)

            if mean == 0:
                return 0.0

            variance = np.var(sizes)

            similarity = 1 - min(variance / (mean + 1e-9), 1)

            return float(similarity)

        return 0.0

    # --------------------------------------------------------
    # Price stagnation detection
    # --------------------------------------------------------

    def _price_stagnation_score(
        self,
        trades: pd.DataFrame,
        last_price: float
    ) -> float:

        if len(trades) < 6:
            return 0.0

        recent = trades.tail(6)

        price_move = abs(
            recent["price"].max() -
            recent["price"].min()
        )

        if last_price == 0:
            return 0.0

        move_ratio = price_move / last_price

        if move_ratio < self.price_move_threshold:

            volume = recent["size"].sum()

            score = min(volume / 20000, 1)

            return float(score)

        return 0.0


# ============================================================
# Singleton
# ============================================================

_detector = None


def get_iceberg_detector() -> IcebergDetector:

    global _detector

    if _detector is None:

        _detector = IcebergDetector()

    return _detector