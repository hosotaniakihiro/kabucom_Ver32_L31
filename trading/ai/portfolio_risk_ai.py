# ============================================================
# trading/ai/portfolio_risk_ai.py
# PRODUCTION PORTFOLIO RISK AI
#
# Controls overall portfolio exposure using:
#
#   correlation
#   index exposure
#   sector concentration
#   total risk
#   max positions
#
# Outputs:
#   allow_trade
#   adjusted_position_value
#   portfolio_risk_score
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
# Portfolio Risk AI
# ============================================================

class PortfolioRiskAI:

    def __init__(self):

        # max simultaneous positions
        self.max_positions = 8

        # max portfolio capital exposure
        self.max_portfolio_fraction = 0.35

        # correlation threshold
        self.correlation_threshold = 0.7

        # sector concentration
        self.max_sector_fraction = 0.25

    # --------------------------------------------------------
    # main evaluation
    # --------------------------------------------------------

    def evaluate(
        self,
        symbol: str,
        position_value: float,
        capital: float,
        portfolio_positions: List[Dict],
        correlation_matrix: Dict,
        sector_map: Dict
    ) -> Dict:

        try:

            current_exposure = self._portfolio_exposure(
                portfolio_positions,
                capital
            )

            correlation_penalty = self._correlation_penalty(
                symbol,
                portfolio_positions,
                correlation_matrix
            )

            sector_penalty = self._sector_penalty(
                symbol,
                portfolio_positions,
                sector_map,
                capital
            )

            position_count_penalty = self._position_penalty(
                portfolio_positions
            )

            risk_score = (

                current_exposure * 0.4
                + correlation_penalty * 0.25
                + sector_penalty * 0.2
                + position_count_penalty * 0.15

            )

            risk_score = _clip(risk_score)

            adjusted_value = position_value * (1 - risk_score)

            allow_trade = risk_score < 0.85

            return {

                "allow_trade": allow_trade,

                "adjusted_position_value": float(adjusted_value),

                "portfolio_risk_score": float(risk_score),

                "current_exposure": float(current_exposure)

            }

        except Exception:

            logger.exception("PortfolioRiskAI failure")

            return {

                "allow_trade": False,

                "adjusted_position_value": 0,

                "portfolio_risk_score": 1

            }

    # --------------------------------------------------------
    # portfolio exposure
    # --------------------------------------------------------

    def _portfolio_exposure(
        self,
        positions,
        capital
    ):

        if capital <= 0:
            return 1

        total = sum(
            p.get("position_value", 0)
            for p in positions
        )

        return total / capital

    # --------------------------------------------------------
    # correlation penalty
    # --------------------------------------------------------

    def _correlation_penalty(
        self,
        symbol,
        positions,
        corr_matrix
    ):

        if not positions:
            return 0

        penalties = []

        for p in positions:

            other = p.get("symbol")

            corr = corr_matrix.get(symbol, {}).get(other, 0)

            if corr > self.correlation_threshold:

                penalties.append(corr)

        if not penalties:
            return 0

        return _clip(np.mean(penalties))

    # --------------------------------------------------------
    # sector penalty
    # --------------------------------------------------------

    def _sector_penalty(
        self,
        symbol,
        positions,
        sector_map,
        capital
    ):

        sector = sector_map.get(symbol)

        if sector is None:
            return 0

        sector_value = 0

        for p in positions:

            if sector_map.get(p.get("symbol")) == sector:

                sector_value += p.get("position_value", 0)

        sector_fraction = sector_value / max(capital, 1)

        if sector_fraction > self.max_sector_fraction:

            return _clip(sector_fraction)

        return 0

    # --------------------------------------------------------
    # position count penalty
    # --------------------------------------------------------

    def _position_penalty(self, positions):

        n = len(positions)

        if n <= self.max_positions:
            return 0

        excess = n - self.max_positions

        return _clip(excess / self.max_positions)


# ============================================================
# Singleton
# ============================================================

_ai = None


def get_portfolio_risk_ai():

    global _ai

    if _ai is None:

        _ai = PortfolioRiskAI()

    return _ai