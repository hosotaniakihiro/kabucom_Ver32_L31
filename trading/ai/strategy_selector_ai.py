# ============================================================
# trading/ai/strategy_selector_ai.py
# PRODUCTION STRATEGY SELECTOR AI
#
# Determines best strategy based on market conditions
#
# Possible strategies:
#
#   momentum
#   mean_reversion
#   breakout
#   scalping
#   liquidity_capture
#   defensive
#
# ============================================================

from __future__ import annotations

import logging
from typing import Dict

logger = logging.getLogger(__name__)


# ============================================================
# Utility
# ============================================================

def _clip(x, lo=0.0, hi=1.0):
    return max(lo, min(x, hi))


# ============================================================
# Strategy Selector AI
# ============================================================

class StrategySelectorAI:

    def __init__(self):

        self.volatility_threshold = 0.02

        self.toxicity_limit = 0.8

        self.momentum_threshold = 0.6

    # --------------------------------------------------------
    # Main selection
    # --------------------------------------------------------

    def select_strategy(
        self,
        regime: Dict,
        microstructure: Dict,
        momentum: float,
        volatility: float,
        liquidity: float
    ) -> Dict:

        try:

            regime_type = regime.get("regime", "UNKNOWN")

            toxicity = microstructure.get(
                "toxicity", {}
            ).get(
                "toxicity", 0
            )

            orderflow = microstructure.get(
                "orderflow", {}
            ).get(
                "orderflow_score", 0
            )

            if toxicity > self.toxicity_limit:

                strategy = "defensive"

            elif regime_type in ("TREND_UP", "TREND_DOWN"):

                if momentum > self.momentum_threshold:

                    strategy = "momentum"

                else:

                    strategy = "breakout"

            elif regime_type == "RANGE":

                strategy = "mean_reversion"

            elif volatility > self.volatility_threshold:

                strategy = "scalping"

            elif liquidity > 50000 and abs(orderflow) > 0.5:

                strategy = "liquidity_capture"

            else:

                strategy = "defensive"

            confidence = self._confidence(
                strategy,
                regime_type,
                momentum,
                volatility
            )

            return {

                "strategy": strategy,

                "confidence": confidence

            }

        except Exception:

            logger.exception("StrategySelectorAI failure")

            return {

                "strategy": "defensive",

                "confidence": 0

            }

    # --------------------------------------------------------
    # Strategy confidence
    # --------------------------------------------------------

    def _confidence(
        self,
        strategy,
        regime,
        momentum,
        volatility
    ):

        score = 0.5

        if strategy == "momentum":

            score += momentum * 0.4

        if strategy == "scalping":

            score += volatility * 5

        if regime in ("TREND_UP", "TREND_DOWN"):

            score += 0.1

        return _clip(score)


# ============================================================
# Singleton
# ============================================================

_ai = None


def get_strategy_selector_ai():

    global _ai

    if _ai is None:

        _ai = StrategySelectorAI()

    return _ai