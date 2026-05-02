# ============================================================
# trading/ai/smart_money_ai.py
# PRODUCTION SMART MONEY DETECTOR
#
# Detects:
#
#   institutional accumulation
#   smart money inflow
#   hidden absorption
#   distribution
#   large player activity
#
# Integrates multiple AI signals
# ============================================================

from __future__ import annotations

import logging
from typing import Dict

from trading.ai.orderflow_imbalance_ai import get_orderflow_imbalance_ai
from trading.ai.orderbook_pressure_ai import get_orderbook_pressure_ai
from trading.ai.institutional_flow_ai import get_institutional_flow_ai
from trading.ai.algo_spike_ai import detect_algo_spike
from trading.ai.vwap_deviation_ai import classify_vwap_signal

logger = logging.getLogger(__name__)


# ============================================================
# utility
# ============================================================

def _clip(x, lo=0.0, hi=1.0):
    return max(lo, min(x, hi))


# ============================================================
# Smart Money AI
# ============================================================

class SmartMoneyAI:

    def __init__(self):

        self.orderflow_ai = get_orderflow_imbalance_ai()
        self.orderbook_ai = get_orderbook_pressure_ai()
        self.inst_flow_ai = get_institutional_flow_ai()

    # --------------------------------------------------------
    # main analysis
    # --------------------------------------------------------

    def analyze(
        self,
        trades,
        orderbook,
        market_row,
        bid_volume,
        ask_volume,
        spread,
        vwap
    ) -> Dict:

        try:

            # --------------------------------------------
            # orderflow
            # --------------------------------------------

            oflow = self.orderflow_ai.analyze(
                trades,
                bid_volume,
                ask_volume,
                spread
            )

            # --------------------------------------------
            # orderbook pressure
            # --------------------------------------------

            obook = self.orderbook_ai.analyze(
                orderbook
            )

            # --------------------------------------------
            # institutional flow
            # --------------------------------------------

            inst = self.inst_flow_ai.analyze(
                trades,
                vwap,
                bid_volume,
                ask_volume
            )

            # --------------------------------------------
            # algo spike
            # --------------------------------------------

            algo_flag, algo_score, _ = detect_algo_spike(
                market_row
            )

            algo_score = algo_score / 10.0

            # --------------------------------------------
            # vwap signal
            # --------------------------------------------

            vwap_signal, vwap_score, _ = classify_vwap_signal(
                market_row
            )

            vwap_score = abs(vwap_score) / 5.0

            # --------------------------------------------
            # smart money score
            # --------------------------------------------

            buy_score = (

                oflow["buy_pressure"] * 0.25
                + obook["buy_pressure"] * 0.25
                + inst["institutional_buy_score"] * 0.30
                + algo_score * 0.10
                + vwap_score * 0.10

            )

            sell_score = (

                oflow["sell_pressure"] * 0.25
                + obook["sell_pressure"] * 0.25
                + inst["institutional_sell_score"] * 0.30
                + algo_score * 0.10
                + vwap_score * 0.10

            )

            buy_score = _clip(buy_score)
            sell_score = _clip(sell_score)

            direction = self._direction(
                buy_score,
                sell_score
            )

            confidence = max(
                buy_score,
                sell_score
            )

            pattern = self._pattern(
                inst,
                oflow,
                obook
            )

            return {

                "smart_money_buy": float(buy_score),

                "smart_money_sell": float(sell_score),

                "direction": direction,

                "confidence": float(confidence),

                "pattern": pattern,

                "components": {

                    "orderflow": oflow,

                    "orderbook": obook,

                    "institutional": inst,

                    "algo_score": float(algo_score),

                    "vwap_score": float(vwap_score)

                }

            }

        except Exception:

            logger.exception(
                "SmartMoneyAI failure"
            )

            return self._no_signal()

    # --------------------------------------------------------
    # direction
    # --------------------------------------------------------

    def _direction(self, buy, sell):

        if buy > sell * 1.2:
            return "SMART_BUY"

        if sell > buy * 1.2:
            return "SMART_SELL"

        return "NEUTRAL"

    # --------------------------------------------------------
    # pattern classification
    # --------------------------------------------------------

    def _pattern(self, inst, oflow, obook):

        if inst["pattern"] == "ABSORPTION":
            return "SMART_ACCUMULATION"

        if oflow["direction"] == "BUY" and obook["direction"] == "BUY":
            return "AGGRESSIVE_BUY"

        if oflow["direction"] == "SELL" and obook["direction"] == "SELL":
            return "AGGRESSIVE_SELL"

        return "MIXED_FLOW"

    # --------------------------------------------------------
    # fallback
    # --------------------------------------------------------

    def _no_signal(self):

        return {

            "smart_money_buy": 0.0,

            "smart_money_sell": 0.0,

            "direction": "NONE",

            "confidence": 0.0,

            "pattern": "NONE",

            "components": {}

        }


# ============================================================
# singleton
# ============================================================

_ai = None


def get_smart_money_ai():

    global _ai

    if _ai is None:

        _ai = SmartMoneyAI()

    return _ai