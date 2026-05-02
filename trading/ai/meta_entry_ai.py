# ============================================================
# trading/ai/meta_entry_ai.py
#
# META ENTRY AI
#
# Integrates all AI modules and produces
# final entry decision
#
# Inputs
#
#   ranking_momentum
#   algo_spike
#   inago
#   vwap_deviation
#   institutional_flow
#   orderbook_pressure
#   market_regime
#
# Output
#
#   entry_signal
#   entry_score
#   confidence
#
# ============================================================

from __future__ import annotations

import logging
import math
from typing import Dict

logger = logging.getLogger(__name__)


# ============================================================
# safe float
# ============================================================

def _safe(v):

    try:

        f = float(v)

        if not math.isfinite(f):
            return 0.0

        return f

    except Exception:

        return 0.0


def _clip(x, lo=0.0, hi=1.0):

    return max(lo, min(x, hi))


# ============================================================
# Meta Entry AI
# ============================================================

class MetaEntryAI:

    def __init__(self):

        self.entry_threshold = 6
        self.strong_threshold = 10

    # --------------------------------------------------------
    # Main decision
    # --------------------------------------------------------

    def evaluate(self, signals: Dict) -> Dict:

        try:

            ranking = _safe(signals.get("ranking_momentum_score"))
            algo = _safe(signals.get("algo_spike_score"))
            inago = _safe(signals.get("inago_score"))
            vwap = _safe(signals.get("vwap_deviation_score"))

            orderbook = _safe(
                signals.get("orderbook_pressure_score")
            )

            institutional = _safe(
                signals.get("institutional_buy_score")
            )

            regime = signals.get("regime", "RANGE")

            score = (

                ranking * 0.25
                + algo * 0.15
                + inago * 0.15
                + vwap * 0.15
                + orderbook * 0.15
                + institutional * 0.15

            )

            regime_modifier = self._regime_modifier(regime)

            score *= regime_modifier

            direction = self._direction(signals)

            signal = self._signal(score)

            confidence = self._confidence(score)

            return {

                "entry_signal": signal,

                "direction": direction,

                "entry_score": float(score),

                "confidence": float(confidence),

                "regime": regime,

                "components": {

                    "ranking": ranking,

                    "algo_spike": algo,

                    "inago": inago,

                    "vwap": vwap,

                    "orderbook": orderbook,

                    "institutional": institutional

                }

            }

        except Exception:

            logger.exception("MetaEntryAI failure")

            return {

                "entry_signal": "NO_TRADE",

                "entry_score": 0,

                "confidence": 0

            }

    # --------------------------------------------------------
    # Regime modifier
    # --------------------------------------------------------

    def _regime_modifier(self, regime):

        table = {

            "TREND_UP": 1.3,

            "TREND_DOWN": 1.2,

            "RANGE": 0.8,

            "VOLATILE": 0.6,

            "ALGO_DOMINATED": 0.5,

            "LOW_LIQUIDITY": 0.4,

            "NEWS_SHOCK": 0.2

        }

        return table.get(regime, 1.0)

    # --------------------------------------------------------
    # Direction
    # --------------------------------------------------------

    def _direction(self, signals):

        buy = _safe(signals.get("institutional_buy_score"))
        sell = _safe(signals.get("institutional_sell_score"))

        orderbook = _safe(
            signals.get("orderbook_pressure_score")
        )

        if buy > sell and orderbook > 0:

            return "LONG"

        if sell > buy and orderbook < 0:

            return "SHORT"

        return "NEUTRAL"

    # --------------------------------------------------------
    # Signal classification
    # --------------------------------------------------------

    def _signal(self, score):

        if score >= self.strong_threshold:

            return "STRONG_ENTRY"

        if score >= self.entry_threshold:

            return "ENTRY"

        return "NO_TRADE"

    # --------------------------------------------------------
    # Confidence
    # --------------------------------------------------------

    def _confidence(self, score):

        conf = score / 12

        return _clip(conf)


# ============================================================
# Singleton
# ============================================================

_ai = None


def get_meta_entry_ai():

    global _ai

    if _ai is None:

        _ai = MetaEntryAI()

    return _ai