# ============================================================
# trading/ai/alpha_fusion_ai.py
# PRODUCTION ALPHA FUSION ENGINE
#
# Combines multiple AI signals into final trading alpha
#
# ============================================================

from __future__ import annotations

import logging
from typing import Dict

from trading.ai.smart_money_ai import get_smart_money_ai
from trading.ai.liquidity_grab_ai import get_liquidity_grab_ai
from trading.ai.market_regime_ai import get_market_regime_ai

logger = logging.getLogger(__name__)


# ============================================================
# Utility
# ============================================================

def _clip(x, lo=0.0, hi=1.0):
    return max(lo, min(x, hi))


# ============================================================
# Alpha Fusion Engine
# ============================================================

class AlphaFusionAI:

    def __init__(self):

        self.smart_money_ai = get_smart_money_ai()
        self.liquidity_grab_ai = get_liquidity_grab_ai()
        self.regime_ai = get_market_regime_ai()

    # --------------------------------------------------------
    # Main alpha generation
    # --------------------------------------------------------

    def generate_alpha(
        self,
        trades,
        orderbook,
        market_row,
        df,
        bid_volume,
        ask_volume,
        spread,
        vwap,
        volume,
        volume_avg,
        high,
        low,
        close
    ) -> Dict:

        try:

            # --------------------------------------------
            # Smart money
            # --------------------------------------------

            smart = self.smart_money_ai.analyze(
                trades,
                orderbook,
                market_row,
                bid_volume,
                ask_volume,
                spread,
                vwap
            )

            # --------------------------------------------
            # Liquidity grab
            # --------------------------------------------

            liquidity = self.liquidity_grab_ai.analyze(
                df,
                volume,
                volume_avg,
                high,
                low,
                close
            )

            # --------------------------------------------
            # Market regime
            # --------------------------------------------

            regime = self.regime_ai.detect(
                df,
                spread,
                bid_volume,
                ask_volume,
                algo_score=0,
                toxicity=0
            )

            # --------------------------------------------
            # score combine
            # --------------------------------------------

            buy_score = (
                smart["smart_money_buy"] * 0.5
                + liquidity["liquidity_grab_buy"] * 0.3
                + regime["regime_score"] * 0.2
            )

            sell_score = (
                smart["smart_money_sell"] * 0.5
                + liquidity["liquidity_grab_sell"] * 0.3
                + regime["regime_score"] * 0.2
            )

            buy_score = _clip(buy_score)
            sell_score = _clip(sell_score)

            direction = self._direction(
                buy_score,
                sell_score
            )

            confidence = max(buy_score, sell_score)

            return {

                "alpha_buy": float(buy_score),

                "alpha_sell": float(sell_score),

                "direction": direction,

                "confidence": float(confidence),

                "components": {

                    "smart_money": smart,

                    "liquidity_grab": liquidity,

                    "market_regime": regime

                }

            }

        except Exception:

            logger.exception("AlphaFusionAI failure")

            return self._no_signal()

    # --------------------------------------------------------
    # Direction
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

            "alpha_buy": 0.0,

            "alpha_sell": 0.0,

            "direction": "NONE",

            "confidence": 0.0,

            "components": {}

        }


# ============================================================
# Singleton
# ============================================================

_ai = None


def get_alpha_fusion_ai():

    global _ai

    if _ai is None:

        _ai = AlphaFusionAI()

    return _ai