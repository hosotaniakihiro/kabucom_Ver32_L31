# ============================================================
# File   : trading/execution/order_management_system.py
# Version: Ver2.0-PRODUCTION-OMS-HARDENED
# ------------------------------------------------------------
# Order Management System
#
# Responsibilities
#   • Order lifecycle management
#   • Position synchronization
#   • RiskEngine integration
#   • ExecutionEngine integration
#   • RL execution agent integration
#   • Partial fill tracking
#   • Cancel / Replace logic
#   • Portfolio state tracking
#   • Thread-safe order book
# ============================================================

from __future__ import annotations

import logging
import time
import threading
import math
from dataclasses import dataclass, field
from typing import Dict, Optional

from trading.execution.execution_engine import get_execution_engine

# Optional components (fail-safe import)
try:
    from trading.risk.risk_engine import risk_engine
except Exception:
    risk_engine = None

try:
    from trading.ai.rl_execution_agent import rl_execution_agent
except Exception:
    rl_execution_agent = None


logger = logging.getLogger(__name__)

execution_engine = get_execution_engine()


# ============================================================
# Utility
# ============================================================

def _safe_float(x, default=0.0):

    try:

        if x is None:
            return default

        v = float(x)

        if math.isnan(v) or math.isinf(v):
            return default

        return v

    except Exception:

        return default


# ============================================================
# Order Status
# ============================================================

class OrderStatus:

    NEW = "NEW"
    SUBMITTED = "SUBMITTED"
    PARTIAL_FILLED = "PARTIAL_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"


# ============================================================
# Order Dataclass
# ============================================================

@dataclass
class Order:

    symbol: str
    side: str
    quantity: int

    order_id: Optional[str] = None
    price: Optional[float] = None

    filled_qty: int = 0
    status: str = OrderStatus.NEW

    create_time: float = field(default_factory=time.time)
    update_time: float = field(default_factory=time.time)

    metadata: dict = field(default_factory=dict)

    # --------------------------------------------------------

    def remaining(self) -> int:

        return max(self.quantity - self.filled_qty, 0)


# ============================================================
# Position
# ============================================================

@dataclass
class Position:

    symbol: str
    quantity: int = 0
    avg_price: float = 0.0

    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0


# ============================================================
# OMS
# ============================================================

class OrderManagementSystem:

    def __init__(self):

        self.orders: Dict[str, Order] = {}
        self.positions: Dict[str, Position] = {}

        self.lock = threading.RLock()

        logger.info("[OMS] initialized")

    # --------------------------------------------------------
    # Submit Order
    # --------------------------------------------------------

    def submit_order(
        self,
        symbol: str,
        side: str,
        quantity: int,
        price: Optional[float] = None,
    ) -> Optional[str]:

        try:

            quantity = int(quantity)

            if quantity <= 0:
                return None

            # ---------------------------------
            # Risk Engine
            # ---------------------------------

            if risk_engine:

                try:

                    if not risk_engine.allow_order(symbol, side, quantity):

                        logger.warning(
                            "[OMS] RiskEngine rejected %s %s %s",
                            symbol,
                            side,
                            quantity,
                        )
                        return None

                except Exception:

                    logger.exception("[OMS] risk engine error")

            # ---------------------------------
            # RL Agent adjustment
            # ---------------------------------

            if rl_execution_agent:

                try:

                    quantity, price = rl_execution_agent.adjust_order(
                        symbol,
                        side,
                        quantity,
                        price,
                    )

                except Exception:

                    logger.exception("[OMS] RL adjust failed")

            # ---------------------------------
            # Send to Execution Engine
            # ---------------------------------

            order_id = execution_engine.send_order(
                symbol=symbol,
                side=side,
                quantity=quantity,
                price=price,
            )

            if not order_id:

                logger.error("[OMS] execution failed")
                return None

            order = Order(
                symbol=symbol,
                side=side,
                quantity=quantity,
                price=price,
                order_id=order_id,
                status=OrderStatus.SUBMITTED,
            )

            with self.lock:

                self.orders[order_id] = order

            logger.info(
                "[OMS] submitted %s %s qty=%s",
                symbol,
                side,
                quantity,
            )

            return order_id

        except Exception:

            logger.exception("[OMS] submit_order failure")
            return None

    # --------------------------------------------------------
    # Cancel Order
    # --------------------------------------------------------

    def cancel_order(self, order_id: str):

        with self.lock:

            order = self.orders.get(order_id)

        if not order:
            return

        try:

            execution_engine.cancel_order(order_id)

            order.status = OrderStatus.CANCELED
            order.update_time = time.time()

            logger.info("[OMS] canceled %s", order_id)

        except Exception:

            logger.exception("[OMS] cancel failed")

    # --------------------------------------------------------
    # Replace Order
    # --------------------------------------------------------

    def replace_order(
        self,
        order_id: str,
        new_price: Optional[float],
    ):

        with self.lock:

            order = self.orders.get(order_id)

        if not order:
            return

        try:

            execution_engine.replace_order(
                order_id,
                new_price,
            )

            order.price = new_price
            order.update_time = time.time()

            logger.info(
                "[OMS] replaced %s price=%s",
                order_id,
                new_price,
            )

        except Exception:

            logger.exception("[OMS] replace failed")

    # --------------------------------------------------------
    # Fill Update
    # --------------------------------------------------------

    def on_fill(
        self,
        order_id: str,
        filled_qty: int,
        fill_price: float,
    ):

        with self.lock:

            order = self.orders.get(order_id)

        if not order:
            return

        filled_qty = int(filled_qty)
        fill_price = _safe_float(fill_price)

        order.filled_qty += filled_qty
        order.update_time = time.time()

        if order.filled_qty < order.quantity:

            order.status = OrderStatus.PARTIAL_FILLED

        else:

            order.status = OrderStatus.FILLED

        self._update_position(
            order.symbol,
            order.side,
            filled_qty,
            fill_price,
        )

        logger.info(
            "[OMS] fill %s qty=%s price=%s",
            order_id,
            filled_qty,
            fill_price,
        )

    # --------------------------------------------------------
    # Position Update
    # --------------------------------------------------------

    def _update_position(
        self,
        symbol: str,
        side: str,
        qty: int,
        price: float,
    ):

        with self.lock:

            pos = self.positions.get(symbol)

            if not pos:

                pos = Position(symbol=symbol)
                self.positions[symbol] = pos

            if side == "BUY":

                new_qty = pos.quantity + qty

                pos.avg_price = (
                    pos.avg_price * pos.quantity
                    + price * qty
                ) / max(new_qty, 1)

                pos.quantity = new_qty

            else:

                pos.quantity -= qty

                if pos.quantity == 0:
                    pos.avg_price = 0

    # --------------------------------------------------------
    # Update Market Price
    # --------------------------------------------------------

    def update_market_price(
        self,
        symbol: str,
        price: float,
    ):

        pos = self.positions.get(symbol)

        if not pos:
            return

        price = _safe_float(price)

        pos.unrealized_pnl = (
            price - pos.avg_price
        ) * pos.quantity

    # --------------------------------------------------------
    # Position Query
    # --------------------------------------------------------

    def get_position(self, symbol: str) -> Optional[Position]:

        return self.positions.get(symbol)

    # --------------------------------------------------------
    # Portfolio Exposure
    # --------------------------------------------------------

    def portfolio_exposure(self) -> float:

        exposure = 0.0

        for pos in self.positions.values():

            exposure += abs(pos.quantity * pos.avg_price)

        return exposure

    # --------------------------------------------------------
    # Open Orders
    # --------------------------------------------------------

    def get_open_orders(self):

        with self.lock:

            return {
                oid: o
                for oid, o in self.orders.items()
                if o.status in (
                    OrderStatus.NEW,
                    OrderStatus.SUBMITTED,
                    OrderStatus.PARTIAL_FILLED,
                )
            }

    # --------------------------------------------------------
    # OMS Status
    # --------------------------------------------------------

    def status(self):

        return {

            "total_orders": len(self.orders),
            "open_orders": len(self.get_open_orders()),
            "positions": len(self.positions),

        }


# ============================================================
# Global OMS Instance
# ============================================================

order_management_system = OrderManagementSystem()