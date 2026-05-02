# ============================================================
# trading/ai/microstructure/algo_detection_ai.py
# PRODUCTION MICROSTRUCTURE DETECTION ENGINE
#
# Detects:
#   HFT activity
#   Iceberg orders
#   Spoofing
#   Orderflow imbalance
#   Market toxicity
#
# Designed for real-time trading systems
# ============================================================

from __future__ import annotations

import numpy as np
import pandas as pd
import logging
from typing import Dict, Any

logger = logging.getLogger(__name__)


# ============================================================
# Utility
# ============================================================

def _safe_div(a, b):
    if b == 0:
        return 0.0
    return float(a) / float(b)


# ============================================================
# Orderflow Imbalance
# ============================================================

def compute_orderflow_imbalance(bid_volume: float, ask_volume: float) -> float:

    total = bid_volume + ask_volume

    if total <= 0:
        return 0.0

    return (bid_volume - ask_volume) / total


# ============================================================
# Iceberg Detector
# ============================================================

class IcebergDetector:

    def __init__(self):

        self.repeat_trade_threshold = 6
        self.size_similarity_threshold = 0.15

    def detect(self, trades: pd.DataFrame) -> float:

        if trades is None or len(trades) < 10:
            return 0.0

        sizes = trades["size"].values

        if len(sizes) < 5:
            return 0.0

        std = np.std(sizes)
        mean = np.mean(sizes)

        if mean == 0:
            return 0.0

        ratio = std / mean

        similarity = max(0.0, 1.0 - ratio)

        repeats = self._count_repeating_sizes(sizes)

        repeat_score = min(repeats / self.repeat_trade_threshold, 1.0)

        score = similarity * 0.6 + repeat_score * 0.4

        return float(min(score, 1.0))

    def _count_repeating_sizes(self, sizes):

        counts = {}

        for s in sizes:

            k = int(round(s))

            counts[k] = counts.get(k, 0) + 1

        max_repeat = max(counts.values())

        return max_repeat


# ============================================================
# Spoof Detector
# ============================================================

class SpoofDetector:

    def __init__(self):

        self.cancel_ratio_threshold = 0.7

    def detect(self, order_events: Dict[str, float]) -> float:

        placed = order_events.get("placed", 0)
        cancelled = order_events.get("cancelled", 0)

        if placed <= 0:
            return 0.0

        cancel_ratio = cancelled / placed

        return float(min(cancel_ratio, 1.0))


# ============================================================
# Trade Pattern Analyzer
# ============================================================

class TradePatternAnalyzer:

    def __init__(self):

        self.large_trade_threshold = 5000

    def analyze(self, trades: pd.DataFrame) -> Dict[str, float]:

        if trades is None or len(trades) == 0:

            return {
                "large_trade_ratio": 0.0,
                "trade_variance": 0.0,
                "trade_frequency": 0.0,
            }

        sizes = trades["size"].values

        large_trades = sizes[sizes > self.large_trade_threshold]

        large_ratio = _safe_div(len(large_trades), len(sizes))

        variance = np.var(sizes)

        frequency = len(trades)

        return {
            "large_trade_ratio": float(large_ratio),
            "trade_variance": float(variance),
            "trade_frequency": float(frequency),
        }


# ============================================================
# Market Toxicity Model
# ============================================================

class ToxicityModel:

    def compute(
        self,
        algo_score: float,
        spoof_score: float,
        iceberg_score: float,
        imbalance: float
    ) -> float:

        score = (
            algo_score * 0.35
            + spoof_score * 0.30
            + iceberg_score * 0.20
            + abs(imbalance) * 0.15
        )

        return float(min(score, 1.0))


# ============================================================
# Main Algo Detection Engine
# ============================================================

class AlgoDetectionAI:

    def __init__(self):

        self.iceberg = IcebergDetector()
        self.spoof = SpoofDetector()
        self.trade_analyzer = TradePatternAnalyzer()
        self.toxicity = ToxicityModel()

        self.update_speed_threshold = 25
        self.cancel_threshold = 0.75

    # --------------------------------------------------------
    # Main detection
    # --------------------------------------------------------

    def detect(
        self,
        trades: pd.DataFrame,
        bid_volume: float,
        ask_volume: float,
        order_events: Dict[str, float],
        board_updates_per_sec: float,
    ) -> Dict[str, Any]:

        try:

            imbalance = compute_orderflow_imbalance(
                bid_volume,
                ask_volume,
            )

            iceberg_score = self.iceberg.detect(trades)

            spoof_score = self.spoof.detect(order_events)

            trade_stats = self.trade_analyzer.analyze(trades)

            cancel_ratio = _safe_div(
                order_events.get("cancelled", 0),
                order_events.get("placed", 1),
            )

            update_score = min(
                board_updates_per_sec / self.update_speed_threshold,
                1.0,
            )

            algo_score = (
                cancel_ratio * 0.35
                + update_score * 0.35
                + trade_stats["large_trade_ratio"] * 0.30
            )

            toxicity = self.toxicity.compute(
                algo_score,
                spoof_score,
                iceberg_score,
                imbalance,
            )

            algo_active = algo_score > 0.7

            return {

                "algo_active": bool(algo_active),

                "algo_score": float(algo_score),

                "toxicity": float(toxicity),

                "iceberg_score": float(iceberg_score),

                "spoof_score": float(spoof_score),

                "orderflow_imbalance": float(imbalance),

                "cancel_ratio": float(cancel_ratio),

                "board_update_score": float(update_score),

                "large_trade_ratio": trade_stats["large_trade_ratio"],

                "trade_variance": trade_stats["trade_variance"],

            }

        except Exception as e:

            logger.exception("AlgoDetectionAI failure")

            return {
                "algo_active": False,
                "algo_score": 0.0,
                "toxicity": 0.0,
            }


# ============================================================
# Singleton
# ============================================================

_algo_detector = None


def get_algo_detector() -> AlgoDetectionAI:

    global _algo_detector

    if _algo_detector is None:

        _algo_detector = AlgoDetectionAI()

    return _algo_detector