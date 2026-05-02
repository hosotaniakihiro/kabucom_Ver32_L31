# ============================================================
# trading/ai/execution_ai.py
#
# AI SMART EXECUTION ENGINE
#
# Order execution AI
#
# Controls
#
#   order timing
#   price placement
#   slippage control
#   TWAP / VWAP execution
#   liquidity seeking
#
# ============================================================

from __future__ import annotations

import logging
import math
import time
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
# Execution AI
# ============================================================

class ExecutionAI:

    def __init__(self):

        self.max_slippage = 0.002

        self.twap_interval = 2

        self.last_execution_time = {}

    # --------------------------------------------------------
    # Main execution decision
    # --------------------------------------------------------

    def decide_order(self, symbol: str, signal: Dict) -> Dict:

        try:

            price = _safe(signal.get("price"))

            bid = _safe(signal.get("bid"))
            ask = _safe(signal.get("ask"))

            spread = ask - bid

            size = _safe(signal.get("size"))

            side = signal.get("side")

            liquidity = _safe(signal.get("liquidity"))

            if price == 0 or size == 0:

                return self._no_order()

            exec_price = self._optimal_price(
                side,
                bid,
                ask,
                spread
            )

            exec_size = self._optimal_size(
                size,
                liquidity
            )

            delay = self._execution_delay(symbol)

            return {

                "symbol": symbol,

                "side": side,

                "price": exec_price,

                "size": exec_size,

                "delay": delay

            }

        except Exception:

            logger.exception("Execution decision failed")

            return self._no_order()

    # --------------------------------------------------------
    # Price placement
    # --------------------------------------------------------

    def _optimal_price(self, side, bid, ask, spread):

        if side == "BUY":

            price = bid + spread * 0.25

        elif side == "SELL":

            price = ask - spread * 0.25

        else:

            price = bid

        return price

    # --------------------------------------------------------
    # Size optimization
    # --------------------------------------------------------

    def _optimal_size(self, size, liquidity):

        if liquidity <= 0:

            return size * 0.5

        if liquidity < 10000:

            return size * 0.4

        if liquidity < 50000:

            return size * 0.7

        return size

    # --------------------------------------------------------
    # TWAP delay
    # --------------------------------------------------------

    def _execution_delay(self, symbol):

        now = time.time()

        last = self.last_execution_time.get(symbol)

        if last is None:

            self.last_execution_time[symbol] = now

            return 0

        diff = now - last

        if diff < self.twap_interval:

            delay = self.twap_interval - diff

        else:

            delay = 0

        self.last_execution_time[symbol] = now

        return delay

    # --------------------------------------------------------
    # Slippage control
    # --------------------------------------------------------

    def slippage_check(self, expected_price, actual_price):

        try:

            expected = _safe(expected_price)

            actual = _safe(actual_price)

            if expected == 0:

                return False

            slip = abs(actual - expected) / expected

            return slip <= self.max_slippage

        except Exception:

            return False

    # --------------------------------------------------------
    # No order
    # --------------------------------------------------------

    def _no_order(self):

        return {

            "symbol": None,

            "side": None,

            "price": 0,

            "size": 0,

            "delay": 0

        }


# ============================================================
# Singleton
# ============================================================

_ai = None


def get_execution_ai():

    global _ai

    if _ai is None:

        _ai = ExecutionAI()

    return _ai