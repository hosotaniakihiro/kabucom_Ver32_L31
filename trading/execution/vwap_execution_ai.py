# ============================================================
# trading/ai/execution/vwap_execution_ai.py
# PRODUCTION VWAP EXECUTION AI
#
# Optimizes order execution relative to VWAP
#
# Features:
#   adaptive aggressiveness
#   microprice adjustment
#   spread control
#   order slicing
#   toxicity aware execution
#
# Output:
#   limit_price
#   slice_size
#   aggressiveness
#   execution_mode
# ============================================================

from __future__ import annotations

import numpy as np
import logging
from typing import Dict

logger = logging.getLogger(__name__)


# ============================================================
# Utility
# ============================================================

def _clip(x, lo, hi):
    return max(lo, min(x, hi))


# ============================================================
# VWAP Execution AI
# ============================================================

class VWAPExecutionAI:

    def __init__(self):

        self.min_aggressiveness = 0.1
        self.max_aggressiveness = 0.9

        self.base_slice_ratio = 0.15

        self.spread_threshold = 0.003

    # --------------------------------------------------------
    # main decision
    # --------------------------------------------------------

    def decide_execution(
        self,
        side: str,
        best_bid: float,
        best_ask: float,
        bid_size: float,
        ask_size: float,
        vwap: float,
        position_size: float,
        spread: float,
        toxicity: float,
    ) -> Dict:

        try:

            mid = (best_bid + best_ask) / 2

            microprice = self._microprice(
                best_bid,
                best_ask,
                bid_size,
                ask_size
            )

            vwap_dev = (mid - vwap) / max(vwap, 1e-9)

            imbalance = self._liquidity_imbalance(
                bid_size,
                ask_size
            )

            aggressiveness = self._compute_aggressiveness(
                vwap_dev,
                imbalance,
                toxicity
            )

            limit_price = self._compute_limit_price(
                side,
                best_bid,
                best_ask,
                microprice,
                spread,
                aggressiveness
            )

            slice_size = self._compute_slice(
                position_size,
                aggressiveness
            )

            execution_mode = self._execution_mode(
                spread,
                toxicity
            )

            return {

                "limit_price": float(limit_price),

                "slice_size": float(slice_size),

                "aggressiveness": float(aggressiveness),

                "execution_mode": execution_mode,

                "microprice": float(microprice),

                "vwap_deviation": float(vwap_dev)

            }

        except Exception:

            logger.exception("VWAPExecutionAI failure")

            return {
                "limit_price": best_bid if side == "BUY" else best_ask,
                "slice_size": position_size,
                "aggressiveness": 0.5,
                "execution_mode": "PASSIVE"
            }

    # --------------------------------------------------------
    # microprice
    # --------------------------------------------------------

    def _microprice(
        self,
        bid,
        ask,
        bid_size,
        ask_size
    ):

        total = bid_size + ask_size

        if total == 0:
            return (bid + ask) / 2

        return (

            bid * ask_size +
            ask * bid_size

        ) / total

    # --------------------------------------------------------
    # liquidity imbalance
    # --------------------------------------------------------

    def _liquidity_imbalance(
        self,
        bid_size,
        ask_size
    ):

        total = bid_size + ask_size

        if total == 0:
            return 0.0

        return (bid_size - ask_size) / total

    # --------------------------------------------------------
    # aggressiveness model
    # --------------------------------------------------------

    def _compute_aggressiveness(
        self,
        vwap_dev,
        imbalance,
        toxicity
    ):

        base = abs(vwap_dev) * 1.2 + abs(imbalance) * 0.8

        toxicity_penalty = toxicity * 0.5

        aggr = base * (1 - toxicity_penalty)

        aggr = _clip(
            aggr,
            self.min_aggressiveness,
            self.max_aggressiveness
        )

        return aggr

    # --------------------------------------------------------
    # limit price
    # --------------------------------------------------------

    def _compute_limit_price(
        self,
        side,
        best_bid,
        best_ask,
        microprice,
        spread,
        aggressiveness
    ):

        mid = (best_bid + best_ask) / 2

        offset = spread * aggressiveness

        if side == "BUY":

            price = microprice + offset

            return min(price, best_ask)

        else:

            price = microprice - offset

            return max(price, best_bid)

    # --------------------------------------------------------
    # slice size
    # --------------------------------------------------------

    def _compute_slice(
        self,
        position_size,
        aggressiveness
    ):

        ratio = self.base_slice_ratio + aggressiveness * 0.5

        return max(1, position_size * ratio)

    # --------------------------------------------------------
    # execution mode
    # --------------------------------------------------------

    def _execution_mode(
        self,
        spread,
        toxicity
    ):

        if toxicity > 0.8:

            return "PASSIVE"

        if spread > self.spread_threshold:

            return "ICEBERG"

        return "ADAPTIVE"


# ============================================================
# Singleton
# ============================================================

_executor = None


def get_vwap_execution_ai():

    global _executor

    if _executor is None:

        _executor = VWAPExecutionAI()

    return _executor