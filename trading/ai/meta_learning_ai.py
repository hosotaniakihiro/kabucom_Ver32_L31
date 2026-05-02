# ============================================================
# trading/ai/meta_learning_ai.py
# PRODUCTION META LEARNING AI
#
# Self-adaptive strategy optimizer
#
# Learns from:
#   trade history
#   win rate
#   pnl distribution
#   market regime performance
#
# Outputs:
#   parameter adjustments
#   strategy bias
# ============================================================

from __future__ import annotations

import logging
import numpy as np
from typing import Dict, List

logger = logging.getLogger(__name__)


# ============================================================
# Utility
# ============================================================

def _clip(x, lo=0.0, hi=1.0):
    return max(lo, min(x, hi))


# ============================================================
# Meta Learning AI
# ============================================================

class MetaLearningAI:

    def __init__(self):

        self.learning_rate = 0.1

        self.min_samples = 30

        self.performance_window = 200

    # --------------------------------------------------------
    # main learning function
    # --------------------------------------------------------

    def learn(
        self,
        trade_history: List[Dict],
        regime_stats: Dict
    ) -> Dict:

        try:

            if len(trade_history) < self.min_samples:

                return {
                    "adjustment": {},
                    "confidence": 0
                }

            win_rate = self._win_rate(trade_history)

            pnl_stats = self._pnl_distribution(trade_history)

            regime_perf = self._regime_performance(
                trade_history,
                regime_stats
            )

            adjustments = self._parameter_adjustment(
                win_rate,
                pnl_stats,
                regime_perf
            )

            confidence = self._confidence(trade_history)

            return {

                "adjustment": adjustments,

                "confidence": confidence,

                "win_rate": win_rate,

                "avg_return": pnl_stats["mean"]

            }

        except Exception:

            logger.exception("MetaLearningAI failure")

            return {
                "adjustment": {}
            }

    # --------------------------------------------------------
    # win rate
    # --------------------------------------------------------

    def _win_rate(self, history):

        wins = sum(1 for t in history if t.get("pnl", 0) > 0)

        return wins / max(len(history), 1)

    # --------------------------------------------------------
    # pnl distribution
    # --------------------------------------------------------

    def _pnl_distribution(self, history):

        pnl = [t.get("pnl", 0) for t in history]

        pnl = np.array(pnl)

        return {

            "mean": float(np.mean(pnl)),

            "std": float(np.std(pnl)),

            "skew": float(
                (np.mean((pnl - pnl.mean())**3))
                / (np.std(pnl)**3 + 1e-9)
            )

        }

    # --------------------------------------------------------
    # regime performance
    # --------------------------------------------------------

    def _regime_performance(
        self,
        history,
        regime_stats
    ):

        perf = {}

        for trade in history:

            regime = trade.get("regime", "UNKNOWN")

            pnl = trade.get("pnl", 0)

            if regime not in perf:

                perf[regime] = []

            perf[regime].append(pnl)

        result = {}

        for r, values in perf.items():

            result[r] = np.mean(values)

        return result

    # --------------------------------------------------------
    # parameter adjustment
    # --------------------------------------------------------

    def _parameter_adjustment(
        self,
        win_rate,
        pnl_stats,
        regime_perf
    ):

        adjustments = {}

        # entry threshold adjustment

        if win_rate < 0.45:

            adjustments["entry_threshold"] = -self.learning_rate

        elif win_rate > 0.6:

            adjustments["entry_threshold"] = self.learning_rate

        # position sizing

        if pnl_stats["std"] > abs(pnl_stats["mean"]) * 3:

            adjustments["risk_reduction"] = 0.2

        # regime bias

        best_regime = max(
            regime_perf,
            key=regime_perf.get,
            default=None
        )

        adjustments["regime_bias"] = best_regime

        return adjustments

    # --------------------------------------------------------
    # confidence
    # --------------------------------------------------------

    def _confidence(self, history):

        n = len(history)

        return _clip(n / self.performance_window)


# ============================================================
# Singleton
# ============================================================

_ai = None


def get_meta_learning_ai():

    global _ai

    if _ai is None:

        _ai = MetaLearningAI()

    return _ai