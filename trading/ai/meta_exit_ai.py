# ============================================================
# trading/ai/meta_exit_ai.py
#
# META EXIT AI
#
# Integrates multiple AI modules and produces
# final exit decision
#
# Output
#
#   exit_signal
#   exit_score
#   confidence
#
# ============================================================

from __future__ import annotations

import logging
import math
from typing import Dict

logger = logging.getLogger(__name__)


# ============================================================
# Utility
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

    try:
        x = float(x)
    except Exception:
        return 0.0

    if not math.isfinite(x):
        return 0.0

    return max(lo, min(x, hi))


# ============================================================
# Meta Exit AI
# ============================================================

class MetaExitAI:

    def __init__(self):

        self.exit_threshold = 5
        self.strong_exit_threshold = 9

        self.stop_loss_threshold = -0.03
        self.trailing_trigger = 0.04

    # --------------------------------------------------------
    # Main evaluation
    # --------------------------------------------------------

    def evaluate(self, signals: Dict) -> Dict:

        try:

            pnl = _safe(signals.get("unrealized_pnl"))

            ranking = _safe(signals.get("ranking_momentum_score"))
            algo = _safe(signals.get("algo_spike_score"))
            inago = _safe(signals.get("inago_score"))
            vwap = _safe(signals.get("vwap_deviation_score"))

            orderbook = _safe(
                signals.get("orderbook_pressure_score")
            )

            institutional_sell = _safe(
                signals.get("institutional_sell_score")
            )

            regime = signals.get("regime", "RANGE")

            exit_score = (

                algo * 0.2
                + abs(vwap) * 0.2
                + abs(orderbook) * 0.2
                + institutional_sell * 0.2
                + abs(inago) * 0.1
                + abs(ranking) * 0.1

            )

            regime_modifier = self._regime_modifier(regime)

            exit_score *= regime_modifier

            signal = self._exit_signal(exit_score, pnl)

            confidence = self._confidence(exit_score)

            return {

                "exit_signal": signal,

                "exit_score": float(exit_score),

                "confidence": float(confidence),

                "pnl": float(pnl),

                "regime": regime,

                "components": {

                    "algo_spike": algo,

                    "vwap": vwap,

                    "orderbook": orderbook,

                    "institutional_sell": institutional_sell,

                    "inago": inago,

                    "ranking": ranking

                }

            }

        except Exception:

            logger.exception("MetaExitAI failure")

            return {

                "exit_signal": "HOLD",

                "exit_score": 0,

                "confidence": 0

            }

    # --------------------------------------------------------
    # Regime modifier
    # --------------------------------------------------------

    def _regime_modifier(self, regime):

        table = {

            "TREND_UP": 0.8,
            "TREND_DOWN": 1.2,
            "RANGE": 1.0,
            "VOLATILE": 1.3,
            "ALGO_DOMINATED": 1.4,
            "LOW_LIQUIDITY": 1.5,
            "NEWS_SHOCK": 1.6

        }

        return table.get(regime, 1.0)

    # --------------------------------------------------------
    # Exit classification
    # --------------------------------------------------------

    def _exit_signal(self, score, pnl):

        if pnl <= self.stop_loss_threshold:

            return "STOP_LOSS"

        if pnl > self.trailing_trigger and score > self.exit_threshold:

            return "TRAIL_EXIT"

        if score >= self.strong_exit_threshold:

            return "STRONG_EXIT"

        if score >= self.exit_threshold:

            return "EXIT"

        return "HOLD"

    # --------------------------------------------------------
    # Confidence
    # --------------------------------------------------------

    def _confidence(self, score):

        conf = score / 10

        return _clip(conf)


# ============================================================
# Singleton
# ============================================================

_ai = None


def get_meta_exit_ai():

    global _ai

    if _ai is None:

        _ai = MetaExitAI()

    return _ai