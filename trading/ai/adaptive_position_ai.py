# ============================================================
# trading/ai/adaptive_position_ai.py
# PRODUCTION ADAPTIVE POSITION AI
#
# Determines optimal position size using:
#
#   alpha score
#   expected return
#   confidence
#   volatility
#   liquidity
#   market regime
#
# Outputs:
#   lot_size
#   risk_multiplier
#   capital_fraction
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
# Adaptive Position AI
# ============================================================

class AdaptivePositionAI:

    def __init__(self):

        # base capital fraction
        self.base_fraction = 0.01

        # max risk
        self.max_fraction = 0.05

        # volatility penalty
        self.vol_penalty = 0.6

        # liquidity scaling
        self.liq_scale = 50000

    # --------------------------------------------------------
    # main sizing function
    # --------------------------------------------------------

    def size_position(
        self,
        alpha: Dict,
        volatility: float,
        liquidity: float,
        regime: Dict,
        capital: float
    ) -> Dict:

        try:

            alpha_score = alpha.get("alpha_score", 0)

            confidence = alpha.get("confidence", 0)

            expected_return = alpha.get("expected_return", 0)

            regime_mult = self._regime_multiplier(regime)

            vol_adj = self._volatility_adjustment(volatility)

            liq_adj = self._liquidity_adjustment(liquidity)

            capital_fraction = (

                self.base_fraction
                * alpha_score
                * confidence
                * regime_mult
                * vol_adj
                * liq_adj

            )

            capital_fraction = min(
                capital_fraction,
                self.max_fraction
            )

            position_value = capital * capital_fraction

            risk_multiplier = self._risk_multiplier(
                alpha_score,
                expected_return
            )

            return {

                "capital_fraction": float(capital_fraction),

                "position_value": float(position_value),

                "risk_multiplier": float(risk_multiplier),

                "alpha_score": alpha_score

            }

        except Exception:

            logger.exception("AdaptivePositionAI failure")

            return {

                "capital_fraction": 0,

                "position_value": 0,

                "risk_multiplier": 0

            }

    # --------------------------------------------------------
    # regime multiplier
    # --------------------------------------------------------

    def _regime_multiplier(self, regime):

        r = regime.get("regime", "UNKNOWN")

        if r == "TREND_UP":
            return 1.4

        if r == "TREND_DOWN":
            return 1.2

        if r == "RANGE":
            return 0.8

        if r == "VOLATILE":
            return 0.7

        if r == "ALGO_DOMINATED":
            return 0.4

        if r == "NEWS_SHOCK":
            return 0.2

        return 1.0

    # --------------------------------------------------------
    # volatility adjustment
    # --------------------------------------------------------

    def _volatility_adjustment(self, volatility):

        if volatility <= 0:
            return 1.0

        penalty = 1 - volatility * self.vol_penalty

        return _clip(penalty, 0.2, 1.0)

    # --------------------------------------------------------
    # liquidity adjustment
    # --------------------------------------------------------

    def _liquidity_adjustment(self, liquidity):

        if liquidity <= 0:
            return 0.3

        score = liquidity / self.liq_scale

        return _clip(score, 0.3, 1.5)

    # --------------------------------------------------------
    # risk multiplier
    # --------------------------------------------------------

    def _risk_multiplier(
        self,
        alpha_score,
        expected_return
    ):

        base = alpha_score * 1.5 + expected_return * 10

        return _clip(base, 0.5, 2.5)


# ============================================================
# Singleton
# ============================================================

_ai = None


def get_adaptive_position_ai():

    global _ai

    if _ai is None:

        _ai = AdaptivePositionAI()

    return _ai