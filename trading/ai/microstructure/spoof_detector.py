# ============================================================
# trading/ai/microstructure/spoof_detector.py
# PRODUCTION SPOOF DETECTOR
#
# Detects:
#   Spoofing
#   Layering
#   Fake liquidity
#   Cancel bursts
#   Short-lived orders
#
# Designed for real-time orderbook environments
# ============================================================

from __future__ import annotations

import numpy as np
import logging
from typing import Dict, List

logger = logging.getLogger(__name__)


# ============================================================
# Utility
# ============================================================

def _safe_div(a, b):
    if b == 0:
        return 0.0
    return float(a) / float(b)


# ============================================================
# Spoof Detector
# ============================================================

class SpoofDetector:

    def __init__(self):

        # cancel ratio threshold
        self.cancel_ratio_threshold = 0.7

        # large order threshold
        self.large_order_size = 5000

        # fast cancel window (seconds)
        self.fast_cancel_window = 1.5

        # layering depth
        self.layer_levels = 4

    # --------------------------------------------------------
    # Main detection
    # --------------------------------------------------------

    def detect(
        self,
        order_events: Dict[str, float],
        large_orders: List[Dict],
        order_lifetimes: List[float],
        board_snapshot: Dict
    ) -> float:

        try:

            cancel_score = self._cancel_ratio(order_events)

            fast_cancel_score = self._fast_cancel_score(order_lifetimes)

            layering_score = self._layering_score(board_snapshot)

            fake_liquidity_score = self._fake_liquidity_score(
                large_orders,
                order_lifetimes
            )

            score = (
                cancel_score * 0.35
                + fast_cancel_score * 0.25
                + layering_score * 0.20
                + fake_liquidity_score * 0.20
            )

            return float(min(score, 1.0))

        except Exception:

            logger.exception("SpoofDetector failure")

            return 0.0

    # --------------------------------------------------------
    # Cancel ratio
    # --------------------------------------------------------

    def _cancel_ratio(self, order_events):

        placed = order_events.get("placed", 0)
        cancelled = order_events.get("cancelled", 0)

        ratio = _safe_div(cancelled, placed)

        return min(ratio, 1.0)

    # --------------------------------------------------------
    # Fast cancel detection
    # --------------------------------------------------------

    def _fast_cancel_score(self, lifetimes: List[float]) -> float:

        if not lifetimes:
            return 0.0

        fast = [x for x in lifetimes if x < self.fast_cancel_window]

        ratio = _safe_div(len(fast), len(lifetimes))

        return float(ratio)

    # --------------------------------------------------------
    # Layering detection
    # --------------------------------------------------------

    def _layering_score(self, board_snapshot: Dict) -> float:

        bids = board_snapshot.get("bids", [])
        asks = board_snapshot.get("asks", [])

        bid_layers = self._layer_pattern(bids)
        ask_layers = self._layer_pattern(asks)

        return max(bid_layers, ask_layers)

    def _layer_pattern(self, side_levels):

        if len(side_levels) < self.layer_levels:
            return 0.0

        sizes = [lvl["size"] for lvl in side_levels[: self.layer_levels]]

        if max(sizes) == 0:
            return 0.0

        variance = np.var(sizes)

        mean = np.mean(sizes)

        if mean == 0:
            return 0.0

        similarity = 1 - min(variance / mean, 1)

        return float(similarity)

    # --------------------------------------------------------
    # Fake liquidity
    # --------------------------------------------------------

    def _fake_liquidity_score(
        self,
        large_orders: List[Dict],
        lifetimes: List[float]
    ) -> float:

        if not large_orders:
            return 0.0

        fake = 0

        for i, order in enumerate(large_orders):

            size = order.get("size", 0)

            if size < self.large_order_size:
                continue

            if i < len(lifetimes):

                life = lifetimes[i]

                if life < self.fast_cancel_window:
                    fake += 1

        ratio = _safe_div(fake, len(large_orders))

        return float(ratio)


# ============================================================
# Singleton
# ============================================================

_detector = None


def get_spoof_detector() -> SpoofDetector:

    global _detector

    if _detector is None:

        _detector = SpoofDetector()

    return _detector