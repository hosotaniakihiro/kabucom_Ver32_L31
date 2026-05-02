# ============================================================
# trading/ai/microstructure/microstructure_feature_engine.py
#
# PRODUCTION MICROSTRUCTURE FEATURE ENGINE
#
# Generates microstructure features from:
#
#   orderbook
#   trades
#   orderflow
#
# Features include:
#
#   orderbook imbalance
#   spread
#   depth ratios
#   trade intensity
#   volume imbalance
#   price pressure
#   volatility microstructure
#
# ============================================================

from __future__ import annotations

import logging
import numpy as np
import pandas as pd
from typing import Dict, List

logger = logging.getLogger(__name__)


# ============================================================
# Utility
# ============================================================

def _clip(x, lo=-1e9, hi=1e9):
    return max(lo, min(x, hi))


def _safe_div(a, b):
    if b == 0:
        return 0
    return a / b


# ============================================================
# Microstructure Feature Engine
# ============================================================

class MicrostructureFeatureEngine:

    def __init__(self):

        self.depth_levels = 5

    # --------------------------------------------------------
    # main feature extraction
    # --------------------------------------------------------

    def compute(
        self,
        orderbook: Dict,
        trades: List[Dict],
        price_series: List[float]
    ) -> Dict:

        try:

            features = {}

            features.update(
                self._spread_features(orderbook)
            )

            features.update(
                self._depth_features(orderbook)
            )

            features.update(
                self._imbalance_features(orderbook)
            )

            features.update(
                self._trade_features(trades)
            )

            features.update(
                self._price_features(price_series)
            )

            features.update(
                self._pressure_features(orderbook, trades)
            )

            return features

        except Exception:

            logger.exception("Feature engine failure")

            return {}

    # --------------------------------------------------------
    # Spread
    # --------------------------------------------------------

    def _spread_features(self, orderbook):

        best_bid = orderbook.get("best_bid")
        best_ask = orderbook.get("best_ask")

        if best_bid is None or best_ask is None:

            return {"spread": 0}

        spread = best_ask - best_bid

        mid = (best_bid + best_ask) / 2

        rel = _safe_div(spread, mid)

        return {

            "spread": spread,

            "relative_spread": rel

        }

    # --------------------------------------------------------
    # Depth features
    # --------------------------------------------------------

    def _depth_features(self, orderbook):

        bids = orderbook.get("bids", [])[:self.depth_levels]
        asks = orderbook.get("asks", [])[:self.depth_levels]

        bid_depth = sum(size for _, size in bids)
        ask_depth = sum(size for _, size in asks)

        return {

            "bid_depth": bid_depth,

            "ask_depth": ask_depth,

            "depth_ratio": _safe_div(bid_depth, ask_depth + 1)

        }

    # --------------------------------------------------------
    # Orderbook imbalance
    # --------------------------------------------------------

    def _imbalance_features(self, orderbook):

        bids = orderbook.get("bids", [])[:self.depth_levels]
        asks = orderbook.get("asks", [])[:self.depth_levels]

        bid_volume = sum(size for _, size in bids)
        ask_volume = sum(size for _, size in asks)

        imbalance = _safe_div(
            bid_volume - ask_volume,
            bid_volume + ask_volume + 1
        )

        return {

            "orderbook_imbalance": imbalance,

            "bid_volume": bid_volume,

            "ask_volume": ask_volume

        }

    # --------------------------------------------------------
    # Trade features
    # --------------------------------------------------------

    def _trade_features(self, trades):

        if not trades:

            return {

                "trade_count": 0,

                "buy_volume": 0,

                "sell_volume": 0

            }

        buy = 0
        sell = 0

        for t in trades:

            size = t.get("size", 0)

            if t.get("side") == "BUY":

                buy += size

            else:

                sell += size

        total = buy + sell

        imbalance = _safe_div(buy - sell, total + 1)

        return {

            "trade_count": len(trades),

            "buy_volume": buy,

            "sell_volume": sell,

            "trade_imbalance": imbalance

        }

    # --------------------------------------------------------
    # Price features
    # --------------------------------------------------------

    def _price_features(self, prices):

        if len(prices) < 3:

            return {

                "micro_volatility": 0,

                "price_momentum": 0

            }

        prices = np.array(prices)

        returns = np.diff(prices)

        vol = np.std(returns)

        momentum = prices[-1] - prices[0]

        return {

            "micro_volatility": float(vol),

            "price_momentum": float(momentum)

        }

    # --------------------------------------------------------
    # Pressure features
    # --------------------------------------------------------

    def _pressure_features(self, orderbook, trades):

        bid_volume = sum(size for _, size in orderbook.get("bids", []))
        ask_volume = sum(size for _, size in orderbook.get("asks", []))

        buy_volume = sum(
            t.get("size", 0)
            for t in trades
            if t.get("side") == "BUY"
        )

        sell_volume = sum(
            t.get("size", 0)
            for t in trades
            if t.get("side") == "SELL"
        )

        buy_pressure = _safe_div(
            buy_volume,
            ask_volume + 1
        )

        sell_pressure = _safe_div(
            sell_volume,
            bid_volume + 1
        )

        return {

            "buy_pressure": buy_pressure,

            "sell_pressure": sell_pressure

        }


# ============================================================
# Singleton
# ============================================================

_engine = None


def get_microstructure_feature_engine():

    global _engine

    if _engine is None:

        _engine = MicrostructureFeatureEngine()

    return _engine