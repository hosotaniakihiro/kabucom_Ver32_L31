# ============================================================
# File   : trading/positions/position_manager.py
# Version: Ver1.0-PRODUCTION-POSITION-MANAGER
# ------------------------------------------------------------
# ✔ entry管理
# ✔ exit管理
# ✔ duplicate entry防止
# ✔ push exit対応
# ✔ AI / scoring対応
# ✔ thread safe
# ✔ real-time safe
# ============================================================

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Dict, Optional
from datetime import datetime

logger = logging.getLogger(__name__)


# ============================================================
# Position Dataclass
# ============================================================

@dataclass
class Position:

    symbol: str
    entry_price: float
    entry_time: datetime
    entry_reason: str

    size: int = 1

    highest_price: float = field(default=0.0)
    lowest_price: float = field(default=0.0)

    ai_score: Optional[float] = None

    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0

    exit_price: Optional[float] = None
    exit_time: Optional[datetime] = None
    exit_reason: Optional[str] = None

    closed: bool = False


# ============================================================
# Position Manager
# ============================================================

class PositionManager:

    def __init__(self):

        self._positions: Dict[str, Position] = {}

        self._lock = threading.Lock()

    # --------------------------------------------------------

    def has_position(self, symbol: str) -> bool:

        with self._lock:

            pos = self._positions.get(symbol)

            if pos is None:
                return False

            return not pos.closed

    # --------------------------------------------------------

    def open_position(
        self,
        symbol: str,
        entry_price: Optional[float],
        entry_reason: str,
        size: int = 1,
        ai_score: Optional[float] = None,
    ) -> Optional[Position]:

        with self._lock:

            if self.has_position(symbol):

                logger.debug(
                    "[POSITION] already open symbol=%s",
                    symbol,
                )

                return None

            if entry_price is None:
                entry_price = 0.0

            pos = Position(
                symbol=symbol,
                entry_price=float(entry_price),
                entry_time=datetime.utcnow(),
                entry_reason=entry_reason,
                size=size,
                ai_score=ai_score,
                highest_price=entry_price,
                lowest_price=entry_price,
            )

            self._positions[symbol] = pos

            logger.info(
                "📈 OPEN POSITION symbol=%s price=%.2f reason=%s",
                symbol,
                entry_price,
                entry_reason,
            )

            return pos

    # --------------------------------------------------------

    def close_position(
        self,
        symbol: str,
        exit_price: float,
        reason: str,
    ) -> Optional[Position]:

        with self._lock:

            pos = self._positions.get(symbol)

            if pos is None or pos.closed:
                return None

            pos.exit_price = float(exit_price)
            pos.exit_time = datetime.utcnow()
            pos.exit_reason = reason
            pos.closed = True

            pos.realized_pnl = (
                (pos.exit_price - pos.entry_price) * pos.size
            )

            logger.info(
                "📉 CLOSE POSITION symbol=%s price=%.2f pnl=%.2f reason=%s",
                symbol,
                exit_price,
                pos.realized_pnl,
                reason,
            )

            return pos

    # --------------------------------------------------------

    def update_price(
        self,
        symbol: str,
        price: float,
    ):

        with self._lock:

            pos = self._positions.get(symbol)

            if pos is None or pos.closed:
                return

            if price > pos.highest_price:
                pos.highest_price = price

            if price < pos.lowest_price:
                pos.lowest_price = price

            pos.unrealized_pnl = (
                (price - pos.entry_price) * pos.size
            )

    # --------------------------------------------------------

    def get_position(self, symbol: str) -> Optional[Position]:

        with self._lock:

            return self._positions.get(symbol)

    # --------------------------------------------------------

    def get_all_positions(self):

        with self._lock:

            return list(self._positions.values())

    # --------------------------------------------------------

    def get_open_positions(self):

        with self._lock:

            return [
                p for p in self._positions.values()
                if not p.closed
            ]

    # --------------------------------------------------------

    def get_closed_positions(self):

        with self._lock:

            return [
                p for p in self._positions.values()
                if p.closed
            ]

    # --------------------------------------------------------

    def position_count(self) -> int:

        with self._lock:

            return len(
                [
                    p for p in self._positions.values()
                    if not p.closed
                ]
            )

    # --------------------------------------------------------

    def total_unrealized_pnl(self) -> float:

        with self._lock:

            return sum(
                p.unrealized_pnl
                for p in self._positions.values()
                if not p.closed
            )

    # --------------------------------------------------------

    def total_realized_pnl(self) -> float:

        with self._lock:

            return sum(
                p.realized_pnl
                for p in self._positions.values()
                if p.closed
            )