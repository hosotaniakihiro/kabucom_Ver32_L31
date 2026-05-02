# ============================================================
# trading/ai/simulation/market_microstructure_simulator.py
# PRODUCTION MARKET MICROSTRUCTURE SIMULATOR
#
# Simulates:
#   order book
#   order flow
#   market impact
#   liquidity
#   slippage
#
# Used for:
#   AI training
#   backtesting
#   execution testing
# ============================================================

from __future__ import annotations

import logging
import random
import numpy as np
from typing import Dict, List

logger = logging.getLogger(__name__)


# ============================================================
# Utility
# ============================================================

def _clip(x, lo, hi):
    return max(lo, min(x, hi))


# ============================================================
# Order Book
# ============================================================

class OrderBook:

    def __init__(self):

        self.bids: List[Dict] = []
        self.asks: List[Dict] = []

    # --------------------------------------------------------
    # initialize book
    # --------------------------------------------------------

    def initialize(self, price):

        for i in range(10):

            self.bids.append({
                "price": price - i * 0.1,
                "size": random.randint(100, 2000)
            })

            self.asks.append({
                "price": price + i * 0.1,
                "size": random.randint(100, 2000)
            })

    # --------------------------------------------------------
    # best bid/ask
    # --------------------------------------------------------

    def best_bid(self):

        return max(self.bids, key=lambda x: x["price"])

    def best_ask(self):

        return min(self.asks, key=lambda x: x["price"])

    # --------------------------------------------------------
    # execute trade
    # --------------------------------------------------------

    def execute_market_order(self, side, size):

        book = self.asks if side == "BUY" else self.bids

        remaining = size

        trades = []

        while remaining > 0 and book:

            level = book[0]

            fill = min(level["size"], remaining)

            trades.append({
                "price": level["price"],
                "size": fill
            })

            level["size"] -= fill

            remaining -= fill

            if level["size"] <= 0:
                book.pop(0)

        return trades


# ============================================================
# Order Flow Generator
# ============================================================

class OrderFlowGenerator:

    def __init__(self):

        self.base_intensity = 5

    def generate(self):

        events = []

        n = np.random.poisson(self.base_intensity)

        for _ in range(n):

            side = random.choice(["BUY", "SELL"])

            size = random.randint(100, 2000)

            events.append({
                "type": "market",
                "side": side,
                "size": size
            })

        return events


# ============================================================
# Market Impact Model
# ============================================================

class MarketImpactModel:

    def __init__(self):

        self.impact_coefficient = 0.0001

    def impact(self, size, liquidity):

        if liquidity <= 0:
            return 0

        return size * self.impact_coefficient / liquidity


# ============================================================
# Liquidity Model
# ============================================================

class LiquidityModel:

    def __init__(self):

        self.decay = 0.95

    def update(self, liquidity):

        noise = random.uniform(-0.1, 0.1)

        return max(0, liquidity * self.decay * (1 + noise))


# ============================================================
# Simulator
# ============================================================

class MarketMicrostructureSimulator:

    def __init__(self):

        self.book = OrderBook()

        self.flow = OrderFlowGenerator()

        self.impact = MarketImpactModel()

        self.liquidity_model = LiquidityModel()

        self.price = 100

        self.liquidity = 50000

    # --------------------------------------------------------
    # initialize
    # --------------------------------------------------------

    def initialize(self, price):

        self.price = price

        self.book.initialize(price)

    # --------------------------------------------------------
    # step simulation
    # --------------------------------------------------------

    def step(self):

        events = self.flow.generate()

        trades = []

        for e in events:

            if e["type"] == "market":

                fills = self.book.execute_market_order(
                    e["side"],
                    e["size"]
                )

                trades.extend(fills)

                impact = self.impact.impact(
                    e["size"],
                    self.liquidity
                )

                if e["side"] == "BUY":

                    self.price += impact

                else:

                    self.price -= impact

        self.liquidity = self.liquidity_model.update(
            self.liquidity
        )

        return {

            "price": self.price,

            "trades": trades,

            "liquidity": self.liquidity

        }

    # --------------------------------------------------------
    # run simulation
    # --------------------------------------------------------

    def run(self, steps=100):

        history = []

        for _ in range(steps):

            state = self.step()

            history.append(state)

        return history


# ============================================================
# Singleton
# ============================================================

_sim = None


def get_market_microstructure_simulator():

    global _sim

    if _sim is None:

        _sim = MarketMicrostructureSimulator()

    return _sim