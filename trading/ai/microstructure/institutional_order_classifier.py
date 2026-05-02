# ============================================================
# trading/ai/microstructure/institutional_order_classifier.py
#
# PRODUCTION ORDER CLASSIFIER
#
# Classifies order origin:
#
#   institutional
#   HFT
#   retail
#   algorithmic
#   spoof
#   iceberg
#
# Used for:
#   flow analysis
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
# Order Classifier
# ============================================================

class InstitutionalOrderClassifier:

    def __init__(self):

        self.large_trade_threshold = 10000

        self.hft_time_threshold = 0.05

        self.cancel_ratio_threshold = 0.7

    # --------------------------------------------------------
    # main classification
    # --------------------------------------------------------

    def classify(
        self,
        trades: List[Dict],
        order_events: List[Dict],
        board_updates: int,
        time_deltas: List[float]
    ) -> Dict:

        try:

            inst_score = self._institutional_score(trades)

            hft_score = self._hft_score(time_deltas)

            retail_score = self._retail_score(trades)

            algo_score = self._algo_score(order_events)

            spoof_score = self._spoof_score(order_events)

            iceberg_score = self._iceberg_score(trades)

            return {

                "institutional": inst_score,

                "hft": hft_score,

                "retail": retail_score,

                "algorithmic": algo_score,

                "spoof": spoof_score,

                "iceberg": iceberg_score

            }

        except Exception:

            logger.exception("Order classification failure")

            return {}

    # --------------------------------------------------------
    # institutional score
    # --------------------------------------------------------

    def _institutional_score(self, trades):

        if not trades:
            return 0

        sizes = [t.get("size", 0) for t in trades]

        large = sum(1 for s in sizes if s > self.large_trade_threshold)

        return _clip(large / len(trades))

    # --------------------------------------------------------
    # hft score
    # --------------------------------------------------------

    def _hft_score(self, time_deltas):

        if not time_deltas:
            return 0

        fast = sum(
            1 for t in time_deltas
            if t < self.hft_time_threshold
        )

        return _clip(fast / len(time_deltas))

    # --------------------------------------------------------
    # retail score
    # --------------------------------------------------------

    def _retail_score(self, trades):

        if not trades:
            return 0

        sizes = [t.get("size", 0) for t in trades]

        small = sum(1 for s in sizes if s < 1000)

        return _clip(small / len(trades))

    # --------------------------------------------------------
    # algo score
    # --------------------------------------------------------

    def _algo_score(self, events):

        if not events:
            return 0

        cancels = sum(
            1 for e in events
            if e.get("type") == "cancel"
        )

        return _clip(cancels / len(events))

    # --------------------------------------------------------
    # spoof score
    # --------------------------------------------------------

    def _spoof_score(self, events):

        if not events:
            return 0

        adds = 0
        cancels = 0

        for e in events:

            if e.get("type") == "add":
                adds += 1

            if e.get("type") == "cancel":
                cancels += 1

        if adds == 0:
            return 0

        ratio = cancels / adds

        if ratio > self.cancel_ratio_threshold:

            return _clip(ratio)

        return 0

    # --------------------------------------------------------
    # iceberg score
    # --------------------------------------------------------

    def _iceberg_score(self, trades):

        if not trades:
            return 0

        price_groups = {}

        for t in trades:

            p = t.get("price")

            size = t.get("size")

            price_groups.setdefault(p, []).append(size)

        iceberg_hits = 0

        for sizes in price_groups.values():

            if len(sizes) > 5 and np.std(sizes) < np.mean(sizes) * 0.2:

                iceberg_hits += 1

        return _clip(iceberg_hits / len(price_groups))


# ============================================================
# Singleton
# ============================================================

_classifier = None


def get_institutional_order_classifier():

    global _classifier

    if _classifier is None:

        _classifier = InstitutionalOrderClassifier()

    return _classifier