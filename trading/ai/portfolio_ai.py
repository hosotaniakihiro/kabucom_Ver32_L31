# ============================================================
# trading/ai/portfolio_ai.py
#
# AI PORTFOLIO OPTIMIZER
#
# Portfolio allocation AI
#
# Controls
#
#   symbol selection
#   capital allocation
#   portfolio diversification
#   correlation risk
#   exposure control
#
# ============================================================

from __future__ import annotations

import logging
import math
from typing import Dict, List

import numpy as np
import pandas as pd

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


def _clip(x, lo, hi):

    try:

        x = float(x)

    except Exception:

        return lo

    if not math.isfinite(x):
        return lo

    return max(lo, min(x, hi))


# ============================================================
# Portfolio AI
# ============================================================

class PortfolioAI:

    def __init__(self):

        self.max_positions = 5

        self.max_single_weight = 0.35

        self.min_signal_threshold = 0.1

    # --------------------------------------------------------
    # Main allocation
    # --------------------------------------------------------

    def allocate(self, candidates: List[Dict]) -> Dict:

        try:

            if not candidates:

                return {}

            scores = []

            for c in candidates:

                score = self._score(c)

                if score > self.min_signal_threshold:

                    scores.append((c["symbol"], score))

            if not scores:

                return {}

            scores.sort(key=lambda x: x[1], reverse=True)

            scores = scores[: self.max_positions]

            total = sum(s for _, s in scores)

            if total == 0:

                return {}

            weights = {}

            for symbol, score in scores:

                w = score / total

                w = _clip(
                    w,
                    0,
                    self.max_single_weight
                )

                weights[symbol] = float(w)

            weights = self._normalize(weights)

            return weights

        except Exception:

            logger.exception("Portfolio allocation failed")

            return {}

    # --------------------------------------------------------
    # Score calculation
    # --------------------------------------------------------

    def _score(self, data: Dict):

        try:

            entry = _safe(data.get("entry_score"))

            regime = _safe(data.get("regime_score"))

            momentum = _safe(
                data.get("ranking_momentum_score")
            )

            flow = _safe(
                data.get("institutional_buy_score")
            )

            pressure = _safe(
                data.get("orderbook_pressure_score")
            )

            risk = _safe(data.get("risk_penalty"))

            score = (

                entry * 0.35
                + regime * 0.15
                + momentum * 0.2
                + flow * 0.2
                + pressure * 0.1
                - risk * 0.3

            )

            return max(score, 0)

        except Exception:

            return 0

    # --------------------------------------------------------
    # Normalize weights
    # --------------------------------------------------------

    def _normalize(self, weights: Dict):

        total = sum(weights.values())

        if total == 0:

            return weights

        return {

            k: float(v / total)

            for k, v in weights.items()

        }

    # --------------------------------------------------------
    # Risk adjustment
    # --------------------------------------------------------

    def adjust_for_volatility(
        self,
        weights: Dict,
        volatilities: Dict
    ):

        try:

            adjusted = {}

            for symbol, weight in weights.items():

                vol = _safe(
                    volatilities.get(symbol)
                )

                if vol > 0:

                    weight = weight / (1 + vol * 10)

                adjusted[symbol] = weight

            return self._normalize(adjusted)

        except Exception:

            return weights

    # --------------------------------------------------------
    # Correlation control
    # --------------------------------------------------------

    def reduce_correlation(
        self,
        weights: Dict,
        correlation_matrix: pd.DataFrame
    ):

        try:

            if correlation_matrix is None:

                return weights

            adjusted = dict(weights)

            for s1 in weights:

                for s2 in weights:

                    if s1 == s2:

                        continue

                    corr = _safe(
                        correlation_matrix.loc[
                            s1, s2
                        ]
                    )

                    if corr > 0.8:

                        adjusted[s2] *= 0.7

            return self._normalize(adjusted)

        except Exception:

            return weights


# ============================================================
# Singleton
# ============================================================

_ai = None


def get_portfolio_ai():

    global _ai

    if _ai is None:

        _ai = PortfolioAI()

    return _ai