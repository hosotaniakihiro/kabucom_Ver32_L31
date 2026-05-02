# ============================================================
# trading/ai/feature_builder_ultra_hft.py
# PRODUCTION ULTRA HFT FEATURE BUILDER
#
# Generates microstructure features for AI trading models
#
# ============================================================

from __future__ import annotations

import logging
import numpy as np
import pandas as pd
from typing import Dict

logger = logging.getLogger(__name__)


# ============================================================
# Utility
# ============================================================

def _safe_div(a, b):
    try:
        if b == 0:
            return 0.0
        return float(a) / float(b)
    except Exception:
        return 0.0


def _safe_std(x):

    try:

        v = np.std(x)

        if np.isnan(v) or np.isinf(v):
            return 0.0

        return float(v)

    except Exception:

        return 0.0


# ============================================================
# Feature Builder
# ============================================================

class UltraHFTFeatureBuilder:

    def __init__(self):

        self.momentum_window = 5
        self.vol_window = 20

    # --------------------------------------------------------
    # main feature generation
    # --------------------------------------------------------

    def build(
        self,
        trades: pd.DataFrame,
        orderbook: Dict,
        df: pd.DataFrame
    ) -> Dict:

        try:

            features = {}

            features["orderflow_imbalance"] = \
                self._orderflow_imbalance(trades)

            features["micro_momentum"] = \
                self._micro_momentum(df)

            features["vwap_deviation"] = \
                self._vwap_deviation(df)

            features["spread"] = \
                self._spread(orderbook)

            features["volume_acceleration"] = \
                self._volume_acceleration(trades)

            features["trade_intensity"] = \
                self._trade_intensity(trades)

            features["liquidity_pressure"] = \
                self._liquidity_pressure(orderbook)

            features["volatility"] = \
                self._volatility(df)

            features["tick_momentum"] = \
                self._tick_momentum(trades)

            return features

        except Exception:

            logger.exception("Feature builder failure")

            return {}

    # --------------------------------------------------------
    # orderflow imbalance
    # --------------------------------------------------------

    def _orderflow_imbalance(self, trades):

        if trades is None or len(trades) == 0:
            return 0.0

        buy = trades.loc[
            trades["side"] == "BUY", "size"
        ].sum()

        sell = trades.loc[
            trades["side"] == "SELL", "size"
        ].sum()

        return _safe_div(buy - sell, buy + sell)

    # --------------------------------------------------------
    # micro momentum
    # --------------------------------------------------------

    def _micro_momentum(self, df):

        if df is None or len(df) < self.momentum_window:
            return 0.0

        prices = df["close"].tail(self.momentum_window)

        return _safe_div(
            prices.iloc[-1] - prices.iloc[0],
            prices.iloc[0]
        )

    # --------------------------------------------------------
    # VWAP deviation
    # --------------------------------------------------------

    def _vwap_deviation(self, df):

        try:

            close = df["close"].iloc[-1]
            vwap = df["vwap"].iloc[-1]

            return _safe_div(close - vwap, vwap)

        except Exception:

            return 0.0

    # --------------------------------------------------------
    # spread
    # --------------------------------------------------------

    def _spread(self, orderbook):

        try:

            bid = orderbook["best_bid"]
            ask = orderbook["best_ask"]

            return ask - bid

        except Exception:

            return 0.0

    # --------------------------------------------------------
    # volume acceleration
    # --------------------------------------------------------

    def _volume_acceleration(self, trades):

        if trades is None or len(trades) < 5:
            return 0.0

        volumes = trades["size"].tail(5)

        return volumes.iloc[-1] - volumes.mean()

    # --------------------------------------------------------
    # trade intensity
    # --------------------------------------------------------

    def _trade_intensity(self, trades):

        if trades is None:
            return 0.0

        return len(trades)

    # --------------------------------------------------------
    # liquidity pressure
    # --------------------------------------------------------

    def _liquidity_pressure(self, orderbook):

        try:

            bid = orderbook["bid_volume"]
            ask = orderbook["ask_volume"]

            return _safe_div(bid - ask, bid + ask)

        except Exception:

            return 0.0

    # --------------------------------------------------------
    # volatility
    # --------------------------------------------------------

    def _volatility(self, df):

        if df is None or len(df) < self.vol_window:
            return 0.0

        returns = df["close"].pct_change().dropna()

        return _safe_std(
            returns.tail(self.vol_window)
        )

    # --------------------------------------------------------
    # tick momentum
    # --------------------------------------------------------

    def _tick_momentum(self, trades):

        if trades is None or len(trades) < 2:
            return 0.0

        prices = trades["price"]

        return prices.iloc[-1] - prices.iloc[0]


# ============================================================
# Singleton
# ============================================================

_builder = None


def get_ultra_hft_feature_builder():

    global _builder

    if _builder is None:

        _builder = UltraHFTFeatureBuilder()

    return _builder