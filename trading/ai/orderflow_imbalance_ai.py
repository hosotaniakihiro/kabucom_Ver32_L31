# ============================================================
# trading/ai/orderflow_imbalance_ai.py
# PRODUCTION ORDERFLOW IMBALANCE AI
#
# Detects:
#
#   bid/ask imbalance
#   aggressive order flow
#   sweep detection
#   liquidity pressure
#   absorption
#
# Designed for microstructure analysis
# ============================================================

from __future__ import annotations

import numpy as np
import pandas as pd
import logging
import math
import threading
from typing import Dict

logger = logging.getLogger(__name__)


# ============================================================
# Utility
# ============================================================

def _clip(x, lo=0.0, hi=1.0):

    try:

        if not math.isfinite(x):

            return 0.0

        return max(lo, min(x, hi))

    except Exception:

        return 0.0


def _safe_div(a, b):

    try:

        if b == 0:

            return 0.0

        v = float(a) / float(b)

        if not math.isfinite(v):

            return 0.0

        return v

    except Exception:

        return 0.0


def _ensure_trades(trades: pd.DataFrame) -> pd.DataFrame:

    try:

        if trades is None or len(trades) == 0:

            return pd.DataFrame(columns=["side", "size"])

        if "side" not in trades.columns:

            trades["side"] = "BUY"

        if "size" not in trades.columns:

            trades["size"] = 0.0

        trades["size"] = pd.to_numeric(
            trades["size"],
            errors="coerce"
        ).fillna(0)

        return trades

    except Exception:

        logger.exception("trade schema fix failure")

        return pd.DataFrame(columns=["side", "size"])


# ============================================================
# OrderFlow Imbalance AI
# ============================================================

class OrderFlowImbalanceAI:

    def __init__(self):

        self.sweep_threshold = 3.0
        self.absorption_threshold = 2.5
        self.imbalance_threshold = 0.6

        self._lock = threading.Lock()

        logger.info("[ORDERFLOW AI] initialized")

    # --------------------------------------------------------
    # Main analyze
    # --------------------------------------------------------

    def analyze(
        self,
        trades: pd.DataFrame,
        bid_volume: float,
        ask_volume: float,
        spread: float
    ) -> Dict:

        try:

            trades = _ensure_trades(trades)

            if trades is None or len(trades) < 5:

                return self._no_signal()

            bid_volume = float(bid_volume)
            ask_volume = float(ask_volume)

            imbalance = self._orderflow_imbalance(
                bid_volume,
                ask_volume
            )

            aggressive = self._aggressive_flow(trades)

            sweep = self._sweep_detection(trades)

            absorption = self._absorption_detection(
                trades,
                bid_volume,
                ask_volume
            )

            liquidity_pressure = self._liquidity_pressure(
                bid_volume,
                ask_volume,
                spread
            )

            # --------------------------------
            # composite pressure
            # --------------------------------

            buy_pressure = (
                imbalance * 0.35
                + aggressive["buy"] * 0.25
                + sweep["buy"] * 0.2
                + absorption["buy"] * 0.2
            )

            sell_pressure = (
                (1 - imbalance) * 0.35
                + aggressive["sell"] * 0.25
                + sweep["sell"] * 0.2
                + absorption["sell"] * 0.2
            )

            buy_pressure = _clip(buy_pressure)
            sell_pressure = _clip(sell_pressure)

            direction = self._direction(
                buy_pressure,
                sell_pressure
            )

            confidence = max(
                buy_pressure,
                sell_pressure
            )

            return {

                "buy_pressure": float(buy_pressure),

                "sell_pressure": float(sell_pressure),

                "direction": direction,

                "confidence": float(confidence),

                "components": {

                    "imbalance": float(imbalance),

                    "aggressive_buy":
                        float(aggressive["buy"]),

                    "aggressive_sell":
                        float(aggressive["sell"]),

                    "sweep_buy":
                        float(sweep["buy"]),

                    "sweep_sell":
                        float(sweep["sell"]),

                    "absorption_buy":
                        float(absorption["buy"]),

                    "absorption_sell":
                        float(absorption["sell"]),

                    "liquidity_pressure":
                        float(liquidity_pressure),

                }

            }

        except Exception:

            logger.exception(
                "[ORDERFLOW AI] analyze failure"
            )

            return self._no_signal()

    # --------------------------------------------------------
    # imbalance
    # --------------------------------------------------------

    def _orderflow_imbalance(
        self,
        bid_volume,
        ask_volume
    ):

        total = bid_volume + ask_volume

        return _safe_div(bid_volume, total)

    # --------------------------------------------------------
    # aggressive flow
    # --------------------------------------------------------

    def _aggressive_flow(self, trades):

        buy = trades.loc[
            trades["side"] == "BUY", "size"
        ].sum()

        sell = trades.loc[
            trades["side"] == "SELL", "size"
        ].sum()

        total = buy + sell

        return {

            "buy": _safe_div(buy, total),

            "sell": _safe_div(sell, total)

        }

    # --------------------------------------------------------
    # sweep detection
    # --------------------------------------------------------

    def _sweep_detection(self, trades):

        sizes = trades["size"]

        mean = sizes.mean()

        if mean == 0 or np.isnan(mean):

            return {"buy": 0.0, "sell": 0.0}

        threshold = mean * self.sweep_threshold

        buy_sweep = trades.loc[
            (trades["size"] > threshold)
            & (trades["side"] == "BUY")
        ]

        sell_sweep = trades.loc[
            (trades["size"] > threshold)
            & (trades["side"] == "SELL")
        ]

        total = len(trades)

        return {

            "buy": _safe_div(len(buy_sweep), total),

            "sell": _safe_div(len(sell_sweep), total)

        }

    # --------------------------------------------------------
    # absorption detection
    # --------------------------------------------------------

    def _absorption_detection(
        self,
        trades,
        bid_volume,
        ask_volume
    ):

        trade_volume = trades["size"].sum()

        liquidity = bid_volume + ask_volume

        if liquidity == 0:

            return {"buy": 0.0, "sell": 0.0}

        ratio = trade_volume / liquidity

        if ratio < self.absorption_threshold:

            return {"buy": 0.0, "sell": 0.0}

        buy = trades.loc[
            trades["side"] == "BUY", "size"
        ].sum()

        sell = trades.loc[
            trades["side"] == "SELL", "size"
        ].sum()

        total = buy + sell

        return {

            "buy": _safe_div(buy, total),

            "sell": _safe_div(sell, total)

        }

    # --------------------------------------------------------
    # liquidity pressure
    # --------------------------------------------------------

    def _liquidity_pressure(
        self,
        bid_volume,
        ask_volume,
        spread
    ):

        total = bid_volume + ask_volume

        imbalance = _safe_div(
            abs(bid_volume - ask_volume),
            total
        )

        spread_penalty = 0

        if spread > 0:

            spread_penalty = min(spread * 0.1, 0.5)

        score = imbalance - spread_penalty

        return _clip(score)

    # --------------------------------------------------------
    # direction
    # --------------------------------------------------------

    def _direction(self, buy, sell):

        if buy > sell * 1.2:

            return "BUY"

        if sell > buy * 1.2:

            return "SELL"

        return "NEUTRAL"

    # --------------------------------------------------------
    # fallback
    # --------------------------------------------------------

    def _no_signal(self):

        return {

            "buy_pressure": 0.0,

            "sell_pressure": 0.0,

            "direction": "NONE",

            "confidence": 0.0,

            "components": {}

        }


# ============================================================
# Singleton
# ============================================================

_ai = None


def get_orderflow_imbalance_ai():

    global _ai

    if _ai is None:

        _ai = OrderFlowImbalanceAI()

    return _ai