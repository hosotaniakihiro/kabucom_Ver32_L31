# ============================================================
# File   : trading/core/trading_engine.py
# Version: Ver1.0-PRODUCTION-TRADING-ENGINE
# ------------------------------------------------------------
# Central Trading Engine
#
# Responsibilities
#   • Market data processing
#   • AI pipeline integration
#   • Order generation
#   • Risk control
#   • Execution coordination
#   • OMS integration
# ============================================================

from __future__ import annotations

import logging
import time
from typing import Dict

from trading.ai.ai_trading_orchestrator import get_ai_trading_orchestrator
from trading.execution.execution_engine import get_execution_engine
from trading.execution.order_management_system import order_management_system
from trading.risk.risk_engine import get_risk_engine

logger = logging.getLogger(__name__)


# ============================================================
# Trading Engine
# ============================================================

class TradingEngine:

    def __init__(self):

        self.orchestrator = get_ai_trading_orchestrator()
        self.execution_engine = get_execution_engine()
        self.risk_engine = get_risk_engine()

        self.last_tick_time = 0

    # --------------------------------------------------------
    # Process Market Tick
    # --------------------------------------------------------

    def process_market_tick(
        self,
        symbol: str,
        orderbook: Dict,
        trades,
        prices,
        portfolio_state: Dict
    ):

        try:

            timestamp = time.time()

            # -------------------------------------------
            # AI Signal
            # -------------------------------------------

            signal = self.orchestrator.process_tick(
                symbol=symbol,
                timestamp=timestamp,
                orderbook=orderbook,
                trades=trades,
                prices=prices,
                portfolio_state=portfolio_state
            )

            action = signal.get("action")

            if action in ("BLOCK", "ERROR", None):
                return

            size = signal.get("size", 0)

            # -------------------------------------------
            # Risk Check
            # -------------------------------------------

            risk = self.risk_engine.evaluate_trade(
                symbol=symbol,
                action=action,
                position_size=size,
                portfolio_risk=portfolio_state.get("risk", 0),
                volatility=portfolio_state.get("volatility", 0),
                drawdown=portfolio_state.get("drawdown", 0),
                daily_pnl=portfolio_state.get("daily_pnl", 0),
                correlation=portfolio_state.get("correlation", 0)
            )

            if not risk.get("allowed"):

                logger.warning(
                    "[ENGINE] Risk blocked trade %s",
                    risk.get("reason")
                )
                return

            size = risk.get("adjusted_size", size)

            # -------------------------------------------
            # Generate Order
            # -------------------------------------------

            order = self.execution_engine.execute(
                symbol=symbol,
                action=action,
                size=size,
                orderbook=orderbook,
                strategy="SMART"
            )

            if not order:
                return

            # -------------------------------------------
            # Submit to OMS
            # -------------------------------------------

            order_management_system.submit_order(
                symbol=order["symbol"],
                side=order["action"],
                quantity=int(order["size"]),
                price=order.get("price")
            )

        except Exception:

            logger.exception("[ENGINE] processing failure")

    # --------------------------------------------------------
    # Update Positions with Market Price
    # --------------------------------------------------------

    def update_market_price(
        self,
        symbol: str,
        price: float
    ):

        order_management_system.update_market_price(
            symbol,
            price
        )

    # --------------------------------------------------------
    # Portfolio State
    # --------------------------------------------------------

    def portfolio_state(self):

        return {

            "exposure": order_management_system.portfolio_exposure(),

            "positions": order_management_system.positions

        }

    # --------------------------------------------------------
    # Health Status
    # --------------------------------------------------------

    def status(self):

        return {

            "engine_running": True,

            "ai_status": self.orchestrator.status(),

            "oms": order_management_system.status(),

        }


# ============================================================
# Global Engine Instance
# ============================================================

trading_engine = TradingEngine()