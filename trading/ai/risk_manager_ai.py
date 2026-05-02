# ============================================================
# trading/ai/risk_manager_ai.py
#
# AI RISK MANAGER
#
# Controls
#
#   position size
#   portfolio exposure
#   max loss
#   volatility scaling
#   liquidity scaling
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


def _clip(x, lo, hi):

    try:

        x = float(x)

    except Exception:

        return lo

    if not math.isfinite(x):

        return lo

    return max(lo, min(x, hi))


# ============================================================
# Risk Manager AI
# ============================================================

class RiskManagerAI:

    def __init__(self):

        self.max_portfolio_risk = 0.05
        self.max_position_risk = 0.02

        self.base_position_size = 1.0

        self.volatility_penalty = 0.5
        self.low_liquidity_penalty = 0.6
        self.news_penalty = 0.3

    # --------------------------------------------------------
    # Main decision
    # --------------------------------------------------------

    def evaluate(self, signals: Dict) -> Dict:

        try:

            regime = signals.get("regime", "RANGE")

            volatility = _safe(signals.get("volatility"))
            liquidity = _safe(signals.get("liquidity"))

            confidence = _safe(signals.get("confidence"))

            entry_score = _safe(signals.get("entry_score"))

            portfolio_risk = _safe(
                signals.get("portfolio_risk")
            )

            position_risk = _safe(
                signals.get("position_risk")
            )

            size = self._position_size(
                entry_score,
                confidence
            )

            size *= self._volatility_modifier(volatility)

            size *= self._liquidity_modifier(liquidity)

            size *= self._regime_modifier(regime)

            allowed = self._risk_allowed(
                portfolio_risk,
                position_risk
            )

            if not allowed:

                size = 0.0

            size = _clip(size, 0.0, 1.0)

            return {

                "position_size": float(size),

                "risk_allowed": bool(allowed),

                "regime": regime,

                "volatility": float(volatility),

                "liquidity": float(liquidity),

                "confidence": float(confidence)

            }

        except Exception:

            logger.exception("RiskManagerAI failure")

            return {

                "position_size": 0.0,

                "risk_allowed": False

            }

    # --------------------------------------------------------
    # Position size base
    # --------------------------------------------------------

    def _position_size(self, entry_score, confidence):

        score_factor = entry_score / 10

        conf_factor = confidence

        size = self.base_position_size * score_factor * conf_factor

        return _clip(size, 0.0, 1.0)

    # --------------------------------------------------------
    # Volatility scaling
    # --------------------------------------------------------

    def _volatility_modifier(self, volatility):

        if volatility > 0.05:

            return self.volatility_penalty

        if volatility > 0.03:

            return 0.7

        return 1.0

    # --------------------------------------------------------
    # Liquidity scaling
    # --------------------------------------------------------

    def _liquidity_modifier(self, liquidity):

        if liquidity < 5000:

            return self.low_liquidity_penalty

        if liquidity < 20000:

            return 0.8

        return 1.0

    # --------------------------------------------------------
    # Regime scaling
    # --------------------------------------------------------

    def _regime_modifier(self, regime):

        table = {

            "TREND_UP": 1.2,
            "TREND_DOWN": 1.1,
            "RANGE": 0.8,
            "VOLATILE": 0.6,
            "ALGO_DOMINATED": 0.5,
            "LOW_LIQUIDITY": 0.4,
            "NEWS_SHOCK": self.news_penalty

        }

        return table.get(regime, 1.0)

    # --------------------------------------------------------
    # Risk checks
    # --------------------------------------------------------

    def _risk_allowed(self, portfolio_risk, position_risk):

        if portfolio_risk > self.max_portfolio_risk:

            return False

        if position_risk > self.max_position_risk:

            return False

        return True


# ============================================================
# Singleton
# ============================================================

_ai = None


def get_risk_manager_ai():

    global _ai

    if _ai is None:

        _ai = RiskManagerAI()

    return _ai