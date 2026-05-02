# ============================================================
# trading/ai/microstructure/latent_liquidity_detector.py
#
# PRODUCTION LATENT LIQUIDITY DETECTOR
#
# Detects hidden liquidity using:
#
#   trade clustering
#   refill patterns
#   price absorption
#   iceberg behavior
#
# Used for:
#   institutional flow detection
#   alpha signal
# ============================================================

from __future__ import annotations

import logging
import numpy as np
from typing import Dict, List

logger = logging.getLogger(__name__)


# ============================================================
# Utility
# ============================================================

def _clip(x, lo=0, hi=1):
    return max(lo, min(x, hi))


# ============================================================
# Latent Liquidity Detector
# ============================================================

class LatentLiquidityDetector:

    def __init__(self):

        self.absorption_threshold = 5

        self.refill_threshold = 3

    # --------------------------------------------------------
    # main detection
    # --------------------------------------------------------

    def detect(
        self,
        trades: List[Dict],
        board_snapshots: List[Dict],
        price_series: List[float]
    ) -> Dict:

        try:

            absorption = self._absorption_score(
                trades,
                price_series
            )

            refill = self._refill_score(
                board_snapshots
            )

            clustering = self._trade_clustering(
                trades
            )

            score = (

                absorption * 0.4
                + refill * 0.3
                + clustering * 0.3

            )

            score = _clip(score)

            direction = self._liquidity_direction(
                trades
            )

            return {

                "latent_liquidity_score": score,

                "direction": direction,

                "absorption": absorption,

                "refill": refill,

                "trade_clustering": clustering

            }

        except Exception:

            logger.exception("Latent liquidity detection failure")

            return {"latent_liquidity_score": 0}

    # --------------------------------------------------------
    # absorption
    # --------------------------------------------------------

    def _absorption_score(
        self,
        trades,
        prices
    ):

        if len(trades) < 5:
            return 0

        sizes = [t.get("size", 0) for t in trades]

        price_change = abs(prices[-1] - prices[0])

        volume = sum(sizes)

        if volume == 0:
            return 0

        absorption = volume / max(price_change, 0.0001)

        if absorption > self.absorption_threshold:

            return _clip(absorption / 10)

        return 0

    # --------------------------------------------------------
    # refill pattern
    # --------------------------------------------------------

    def _refill_score(
        self,
        snapshots
    ):

        if len(snapshots) < 2:
            return 0

        refills = 0

        for i in range(1, len(snapshots)):

            prev = snapshots[i - 1]

            curr = snapshots[i]

            prev_bid = prev.get("best_bid_size", 0)

            curr_bid = curr.get("best_bid_size", 0)

            if curr_bid > prev_bid:

                refills += 1

        return _clip(refills / len(snapshots))

    # --------------------------------------------------------
    # trade clustering
    # --------------------------------------------------------

    def _trade_clustering(
        self,
        trades
    ):

        if len(trades) < 10:
            return 0

        sizes = np.array([
            t.get("size", 0)
            for t in trades
        ])

        if sizes.mean() == 0:
            return 0

        cluster_score = 1 - (np.std(sizes) / sizes.mean())

        return _clip(cluster_score)

    # --------------------------------------------------------
    # liquidity direction
    # --------------------------------------------------------

    def _liquidity_direction(
        self,
        trades
    ):

        buy = sum(
            t.get("size", 0)
            for t in trades
            if t.get("side") == "BUY"
        )

        sell = sum(
            t.get("size", 0)
            for t in trades
            if t.get("side") == "SELL"
        )

        if buy > sell:

            return "BUY"

        if sell > buy:

            return "SELL"

        return "NEUTRAL"


# ============================================================
# Singleton
# ============================================================

_detector = None


def get_latent_liquidity_detector():

    global _detector

    if _detector is None:

        _detector = LatentLiquidityDetector()

    return _detector