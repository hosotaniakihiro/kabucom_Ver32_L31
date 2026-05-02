# ============================================================
# trading/ai/microstructure/hidden_order_inference_ai.py
#
# PRODUCTION HIDDEN ORDER INFERENCE AI
#
# Detects parent orders behind child trades
#
# Techniques:
#   time clustering
#   size regularity
#   price tracking
#   volume slicing detection
#
# Used for:
#   institutional detection
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
# Hidden Order Inference AI
# ============================================================

class HiddenOrderInferenceAI:

    def __init__(self):

        self.time_cluster_threshold = 0.5

        self.size_similarity_threshold = 0.25

    # --------------------------------------------------------
    # main inference
    # --------------------------------------------------------

    def infer(
        self,
        trades: List[Dict]
    ) -> Dict:

        try:

            if len(trades) < 10:

                return {"hidden_order_score": 0}

            time_cluster = self._time_clustering(trades)

            size_pattern = self._size_pattern(trades)

            price_tracking = self._price_tracking(trades)

            slicing = self._volume_slicing(trades)

            score = (

                time_cluster * 0.3
                + size_pattern * 0.3
                + price_tracking * 0.2
                + slicing * 0.2

            )

            direction = self._direction(trades)

            return {

                "hidden_order_score": _clip(score),

                "direction": direction,

                "time_cluster": time_cluster,

                "size_pattern": size_pattern,

                "price_tracking": price_tracking,

                "volume_slicing": slicing

            }

        except Exception:

            logger.exception("Hidden order inference failure")

            return {"hidden_order_score": 0}

    # --------------------------------------------------------
    # time clustering
    # --------------------------------------------------------

    def _time_clustering(self, trades):

        times = [t.get("time", 0) for t in trades]

        if len(times) < 2:
            return 0

        deltas = np.diff(times)

        fast = sum(d < self.time_cluster_threshold for d in deltas)

        return _clip(fast / len(deltas))

    # --------------------------------------------------------
    # size pattern
    # --------------------------------------------------------

    def _size_pattern(self, trades):

        sizes = np.array([t.get("size", 0) for t in trades])

        if sizes.mean() == 0:
            return 0

        variability = np.std(sizes) / sizes.mean()

        if variability < self.size_similarity_threshold:

            return _clip(1 - variability)

        return 0

    # --------------------------------------------------------
    # price tracking
    # --------------------------------------------------------

    def _price_tracking(self, trades):

        prices = np.array([t.get("price", 0) for t in trades])

        if len(prices) < 2:
            return 0

        trend = abs(prices[-1] - prices[0])

        variability = np.std(prices)

        if variability == 0:
            return 1

        score = trend / variability

        return _clip(score)

    # --------------------------------------------------------
    # volume slicing
    # --------------------------------------------------------

    def _volume_slicing(self, trades):

        sizes = [t.get("size", 0) for t in trades]

        groups = {}

        for s in sizes:

            groups.setdefault(s, 0)

            groups[s] += 1

        max_group = max(groups.values())

        return _clip(max_group / len(trades))

    # --------------------------------------------------------
    # direction
    # --------------------------------------------------------

    def _direction(self, trades):

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

_ai = None


def get_hidden_order_inference_ai():

    global _ai

    if _ai is None:

        _ai = HiddenOrderInferenceAI()

    return _ai