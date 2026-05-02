# ============================================================
# trading/ai/market_simulator_ai.py
#
# AI MARKET SIMULATOR
#
# Simulated market environment for AI testing
#
# Features
#
#   price formation
#   orderbook simulation
#   liquidity model
#   trade matching
#   random order flow
#
# ============================================================

from __future__ import annotations

import logging
import math
import random
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


# ============================================================
# OrderBook
# ============================================================

class SimulatedOrderBook:

    def __init__(self, mid_price: float):

        self.mid_price = mid_price

        self.bids = []

        self.asks = []

        self.spread = 0.01 * mid_price

        self._init_book()

    def _init_book(self):

        for i in range(10):

            price = self.mid_price - i * self.spread

            size = random.uniform(100, 1000)

            self.bids.append((price, size))

        for i in range(10):

            price = self.mid_price + i * self.spread

            size = random.uniform(100, 1000)

            self.asks.append((price, size))

    def best_bid(self):

        return max(self.bids, key=lambda x: x[0])[0]

    def best_ask(self):

        return min(self.asks, key=lambda x: x[0])[0]

    def mid(self):

        return (self.best_bid() + self.best_ask()) / 2


# ============================================================
# Market Simulator
# ============================================================

class MarketSimulatorAI:

    def __init__(self):

        self.price = 100.0

        self.book = SimulatedOrderBook(self.price)

        self.trade_history: List[Dict] = []

    # --------------------------------------------------------
    # Step simulation
    # --------------------------------------------------------

    def step(self):

        try:

            order_flow = self._generate_order_flow()

            self._apply_order_flow(order_flow)

            price = self.book.mid()

            self.price = price

            return {

                "price": price,

                "bid": self.book.best_bid(),

                "ask": self.book.best_ask(),

                "spread": self.book.best_ask() - self.book.best_bid()

            }

        except Exception:

            logger.exception("Simulation step failed")

            return {}

    # --------------------------------------------------------
    # Generate random order flow
    # --------------------------------------------------------

    def _generate_order_flow(self):

        orders = []

        n = random.randint(1, 10)

        for _ in range(n):

            side = random.choice(["BUY", "SELL"])

            size = random.uniform(10, 200)

            price = self.price * (1 + random.uniform(-0.002, 0.002))

            orders.append({

                "side": side,

                "price": price,

                "size": size

            })

        return orders

    # --------------------------------------------------------
    # Apply order flow
    # --------------------------------------------------------

    def _apply_order_flow(self, orders):

        for order in orders:

            side = order["side"]

            price = order["price"]

            size = order["size"]

            if side == "BUY":

                self.book.bids.append((price, size))

            else:

                self.book.asks.append((price, size))

            self._match_orders()

    # --------------------------------------------------------
    # Matching engine
    # --------------------------------------------------------

    def _match_orders(self):

        try:

            bids = sorted(self.book.bids, key=lambda x: -x[0])

            asks = sorted(self.book.asks, key=lambda x: x[0])

            new_bids = []
            new_asks = []

            i = 0
            j = 0

            while i < len(bids) and j < len(asks):

                bid_price, bid_size = bids[i]
                ask_price, ask_size = asks[j]

                if bid_price >= ask_price:

                    trade_size = min(bid_size, ask_size)

                    trade_price = (bid_price + ask_price) / 2

                    self.trade_history.append({

                        "price": trade_price,

                        "size": trade_size

                    })

                    bid_size -= trade_size
                    ask_size -= trade_size

                    if bid_size > 0:

                        new_bids.append((bid_price, bid_size))

                    if ask_size > 0:

                        new_asks.append((ask_price, ask_size))

                    i += 1
                    j += 1

                else:

                    new_bids.append((bid_price, bid_size))

                    i += 1

            new_bids.extend(bids[i:])
            new_asks.extend(asks[j:])

            self.book.bids = new_bids
            self.book.asks = new_asks

        except Exception:

            logger.exception("Matching failed")

    # --------------------------------------------------------
    # Run simulation
    # --------------------------------------------------------

    def run(self, steps=1000):

        prices = []

        for _ in range(steps):

            state = self.step()

            prices.append(state.get("price"))

        df = pd.DataFrame({

            "price": prices

        })

        return df


# ============================================================
# Singleton
# ============================================================

_ai = None


def get_market_simulator_ai():

    global _ai

    if _ai is None:

        _ai = MarketSimulatorAI()

    return _ai