# ============================================================
# trading/ai/microstructure/orderbook_reconstruction_engine.py
# PRODUCTION ORDERBOOK RECONSTRUCTION ENGINE
#
# Reconstructs order book from:
#
#   trades
#   order events
#   board snapshots
#
# Used for:
#   microstructure analysis
#   orderflow AI
#   simulation
# ============================================================

from __future__ import annotations

import logging
from typing import Dict, List

logger = logging.getLogger(__name__)


# ============================================================
# OrderBook
# ============================================================

class ReconstructedOrderBook:

    def __init__(self):

        self.bids: Dict[float, float] = {}

        self.asks: Dict[float, float] = {}

    # --------------------------------------------------------
    # best bid
    # --------------------------------------------------------

    def best_bid(self):

        if not self.bids:
            return None

        return max(self.bids.keys())

    # --------------------------------------------------------
    # best ask
    # --------------------------------------------------------

    def best_ask(self):

        if not self.asks:
            return None

        return min(self.asks.keys())

    # --------------------------------------------------------
    # update level
    # --------------------------------------------------------

    def update(self, side: str, price: float, size: float):

        book = self.bids if side == "BUY" else self.asks

        if size <= 0:

            if price in book:

                del book[price]

        else:

            book[price] = size

    # --------------------------------------------------------
    # remove liquidity
    # --------------------------------------------------------

    def consume(self, side: str, size: float):

        book = self.asks if side == "BUY" else self.bids

        remaining = size

        prices = sorted(book.keys())

        if side == "SELL":

            prices = list(reversed(prices))

        trades = []

        for p in prices:

            if remaining <= 0:

                break

            level = book[p]

            fill = min(level, remaining)

            trades.append({
                "price": p,
                "size": fill
            })

            book[p] -= fill

            remaining -= fill

            if book[p] <= 0:

                del book[p]

        return trades


# ============================================================
# Reconstruction Engine
# ============================================================

class OrderBookReconstructionEngine:

    def __init__(self):

        self.book = ReconstructedOrderBook()

    # --------------------------------------------------------
    # initialize snapshot
    # --------------------------------------------------------

    def load_snapshot(self, snapshot: Dict):

        bids = snapshot.get("bids", [])

        asks = snapshot.get("asks", [])

        for price, size in bids:

            self.book.update("BUY", price, size)

        for price, size in asks:

            self.book.update("SELL", price, size)

    # --------------------------------------------------------
    # process order events
    # --------------------------------------------------------

    def process_events(self, events: List[Dict]):

        for e in events:

            etype = e.get("type")

            side = e.get("side")

            price = e.get("price")

            size = e.get("size", 0)

            if etype == "add":

                self.book.update(side, price, size)

            elif etype == "cancel":

                self.book.update(side, price, 0)

            elif etype == "modify":

                self.book.update(side, price, size)

            elif etype == "trade":

                self.book.consume(side, size)

    # --------------------------------------------------------
    # reconstruct from trades
    # --------------------------------------------------------

    def apply_trades(self, trades: List[Dict]):

        for t in trades:

            side = t.get("side")

            size = t.get("size")

            self.book.consume(side, size)

    # --------------------------------------------------------
    # get orderbook snapshot
    # --------------------------------------------------------

    def snapshot(self):

        return {

            "bids": sorted(
                self.book.bids.items(),
                key=lambda x: -x[0]
            ),

            "asks": sorted(
                self.book.asks.items(),
                key=lambda x: x[0]
            )

        }

    # --------------------------------------------------------
    # best bid/ask
    # --------------------------------------------------------

    def best_prices(self):

        return {

            "best_bid": self.book.best_bid(),

            "best_ask": self.book.best_ask()

        }


# ============================================================
# Singleton
# ============================================================

_engine = None


def get_orderbook_reconstruction_engine():

    global _engine

    if _engine is None:

        _engine = OrderBookReconstructionEngine()

    return _engine