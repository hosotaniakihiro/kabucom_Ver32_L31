# ============================================================
# risk_guardian_ai.py
#
# AI RISK GUARDIAN
#
# Protects trading system
#
# ============================================================

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class RiskGuardianAI:

    def __init__(self):

        self.daily_loss_limit = -0.05

        self.max_positions = 10

        self.max_single_loss = -0.02

        self.equity = 1.0

        self.positions = 0

    # --------------------------------------------------------
    # Update equity
    # --------------------------------------------------------

    def update_equity(self, pnl):

        self.equity += pnl

    # --------------------------------------------------------
    # Check trade allowed
    # --------------------------------------------------------

    def allow_trade(self):

        if self.equity < 1 + self.daily_loss_limit:

            logger.error("Daily loss limit reached")

            return False

        if self.positions >= self.max_positions:

            logger.warning("Max positions reached")

            return False

        return True

    # --------------------------------------------------------
    # Check stop loss
    # --------------------------------------------------------

    def check_position(self, pnl):

        if pnl < self.max_single_loss:

            logger.warning("Position stop triggered")

            return True

        return False

    # --------------------------------------------------------
    # Register position
    # --------------------------------------------------------

    def open_position(self):

        self.positions += 1

    # --------------------------------------------------------
    # Close position
    # --------------------------------------------------------

    def close_position(self):

        if self.positions > 0:

            self.positions -= 1


_guardian = None


def get_risk_guardian_ai():

    global _guardian

    if _guardian is None:

        _guardian = RiskGuardianAI()

    return _guardian