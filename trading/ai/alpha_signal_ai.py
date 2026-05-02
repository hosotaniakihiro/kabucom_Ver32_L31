# ============================================================
# trading/ai/alpha_signal_ai.py
# PRODUCTION ALPHA SIGNAL ENGINE
#
# Final AI layer
#
# Combines:
#   market regime
#   microstructure
#   institutional flow
#   ranking / score
#   momentum
#
# Outputs:
#   entry_probability
#   expected_return
#   confidence
#   lot_multiplier
#   allow_trade
# ============================================================

from __future__ import annotations

import logging
import numpy as np
from typing import Dict

logger = logging.getLogger(__name__)


# ============================================================
# Utility
# ============================================================

def _clip(x, lo=0.0, hi=1.0):
    return max(lo, min(x, hi))


# ============================================================
# Alpha Signal AI
# ============================================================

class AlphaSignalAI:

    def __init__(self):

        # weights
        self.w_regime = 0.2
        self.w_micro = 0.25
        self.w_flow = 0.2
        self.w_score = 0.25
        self.w_momentum = 0.1

        self.entry_threshold = 0.55

    # --------------------------------------------------------
    # Main decision
    # --------------------------------------------------------

    def evaluate(
        self,
        analysis: Dict,
        score: float,
        momentum: float
    ) -> Dict:

        try:

            regime_score = self._regime_score(
                analysis.get("regime", {})
            )

            micro_score = self._microstructure_score(
                analysis.get("microstructure", {})
            )

            flow_score = self._flow_score(
                analysis.get("institutional_flow", {})
            )

            score_norm = _clip(score / 10)

            momentum_score = _clip(abs(momentum) * 5)

            alpha = (

                regime_score * self.w_regime
                + micro_score * self.w_micro
                + flow_score * self.w_flow
                + score_norm * self.w_score
                + momentum_score * self.w_momentum

            )

            alpha = _clip(alpha)

            expected_return = self._expected_return(alpha)

            confidence = self._confidence(alpha)

            lot_multiplier = self._lot_multiplier(alpha)

            allow_trade = alpha > self.entry_threshold

            return {

                "alpha_score": float(alpha),

                "entry_probability": float(alpha),

                "expected_return": float(expected_return),

                "confidence": float(confidence),

                "lot_multiplier": float(lot_multiplier),

                "allow_trade": bool(allow_trade)

            }

        except Exception:

            logger.exception("AlphaSignalAI failure")

            return {

                "alpha_score": 0,

                "allow_trade": False

            }

    # --------------------------------------------------------
    # Regime score
    # --------------------------------------------------------

    def _regime_score(self, regime_data):

        regime = regime_data.get("regime", "UNKNOWN")

        if regime == "TREND_UP":
            return 0.9

        if regime == "TREND_DOWN":
            return 0.7

        if regime == "RANGE":
            return 0.4

        if regime == "VOLATILE":
            return 0.3

        if regime == "ALGO_DOMINATED":
            return 0.1

        if regime == "NEWS_SHOCK":
            return 0.0

        return 0.5

    # --------------------------------------------------------
    # Microstructure score
    # --------------------------------------------------------

    def _microstructure_score(self, micro):

        toxicity = micro.get("toxicity", {}).get("toxicity", 0)

        iceberg = micro.get("iceberg", 0)

        spoof = micro.get("spoof", 0)

        algo = micro.get("algo", {}).get("algo_score", 0)

        score = (

            (1 - toxicity) * 0.5
            + iceberg * 0.2
            + (1 - spoof) * 0.15
            + (1 - algo) * 0.15

        )

        return _clip(score)

    # --------------------------------------------------------
    # Institutional flow score
    # --------------------------------------------------------

    def _flow_score(self, flow):

        buy = flow.get("institutional_buy_score", 0)

        sell = flow.get("institutional_sell_score", 0)

        direction = flow.get("flow_direction", "NONE")

        if direction == "BUY":
            return _clip(buy)

        if direction == "SELL":
            return _clip(sell)

        return 0.5

    # --------------------------------------------------------
    # Expected return
    # --------------------------------------------------------

    def _expected_return(self, alpha):

        return alpha * 0.03

    # --------------------------------------------------------
    # Confidence
    # --------------------------------------------------------

    def _confidence(self, alpha):

        return _clip(alpha * 1.2)

    # --------------------------------------------------------
    # Lot multiplier
    # --------------------------------------------------------

    def _lot_multiplier(self, alpha):

        if alpha > 0.8:
            return 2.0

        if alpha > 0.7:
            return 1.5

        if alpha > 0.6:
            return 1.2

        return 1.0


# ============================================================
# Singleton
# ============================================================

_ai = None


def get_alpha_signal_ai():

    global _ai

    if _ai is None:

        _ai = AlphaSignalAI()

    return _ai