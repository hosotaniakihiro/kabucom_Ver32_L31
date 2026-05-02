# ============================================================
# trading/ai/risk_engine.py
#
# PRODUCTION RISK ENGINE
#
# Controls:
#   position limits
#   drawdown protection
#   volatility exposure
#   portfolio risk
#
# Final safety layer before execution
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
# Risk Engine
# ============================================================

class RiskEngine:

    def __init__(self):

        # portfolio limits
        self.max_portfolio_risk = 0.05

        # position limits
        self.max_position_size = 0.1

        # daily loss limit
        self.daily_loss_limit = -0.03

        # drawdown limit
        self.max_drawdown = -0.08

        # volatility threshold
        self.volatility_limit = 0.03

        # correlation threshold
        self.correlation_limit = 0.8

    # --------------------------------------------------------
    # evaluate trade
    # --------------------------------------------------------

    def evaluate_trade(
        self,
        symbol: str,
        action: str,
        position_size: float,
        portfolio_risk: float,
        volatility: float,
        drawdown: float,
        daily_pnl: float,
        correlation: float
    ) -> Dict:

        try:

            # drawdown protection
            if drawdown < self.max_drawdown:

                return self._block("MAX_DRAWDOWN")

            # daily loss protection
            if daily_pnl < self.daily_loss_limit:

                return self._block("DAILY_LOSS_LIMIT")

            # portfolio exposure
            if portfolio_risk > self.max_portfolio_risk:

                return self._block("PORTFOLIO_RISK")

            # position size
            if position_size > self.max_position_size:

                return self._reduce(position_size)

            # volatility protection
            if volatility > self.volatility_limit:

                return self._reduce(position_size)

            # correlation risk
            if correlation > self.correlation_limit:

                return self._reduce(position_size)

            return {

                "allowed": True,

                "adjusted_size": position_size

            }

        except Exception:

            logger.exception("Risk evaluation failure")

            return self._block("RISK_ENGINE_ERROR")

    # --------------------------------------------------------
    # reduce position
    # --------------------------------------------------------

    def _reduce(self, size):

        new_size = size * 0.5

        return {

            "allowed": True,

            "adjusted_size": new_size

        }

    # --------------------------------------------------------
    # block trade
    # --------------------------------------------------------

    def _block(self, reason):

        return {

            "allowed": False,

            "reason": reason

        }

    # --------------------------------------------------------
    # portfolio risk
    # --------------------------------------------------------

    def portfolio_risk(
        self,
        positions: Dict,
        volatilities: Dict
    ):

        risk = 0

        for sym, size in positions.items():

            vol = volatilities.get(sym, 0)

            risk += abs(size) * vol

        return float(risk)

    # --------------------------------------------------------
    # correlation estimate
    # --------------------------------------------------------

    def correlation(
        self,
        returns_matrix
    ):

        try:

            corr = np.corrcoef(returns_matrix)

            return float(np.mean(np.abs(corr)))

        except Exception:

            return 0.0


# ============================================================
# Singleton
# ============================================================

_engine = None


def get_risk_engine():

    global _engine

    if _engine is None:

        _engine = RiskEngine()

    return _engine