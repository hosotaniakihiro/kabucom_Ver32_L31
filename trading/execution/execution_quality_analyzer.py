# ============================================================
# trading/ai/execution/execution_quality_analyzer.py
# PRODUCTION EXECUTION QUALITY ANALYZER
#
# Evaluates execution performance using:
#
#   slippage
#   vwap deviation
#   market impact
#   fill speed
#   liquidity consumption
#
# Used for:
#   execution optimization
#   AI training
#   execution monitoring
# ============================================================

from __future__ import annotations

import logging
import numpy as np
from typing import Dict, List

logger = logging.getLogger(__name__)


# ============================================================
# Utility
# ============================================================

def _safe_div(a, b):
    if b == 0:
        return 0
    return a / b


def _clip(x, lo=0.0, hi=1.0):
    return max(lo, min(x, hi))


# ============================================================
# Execution Quality Analyzer
# ============================================================

class ExecutionQualityAnalyzer:

    def __init__(self):

        self.slippage_weight = 0.3
        self.vwap_weight = 0.3
        self.impact_weight = 0.2
        self.speed_weight = 0.1
        self.liquidity_weight = 0.1

    # --------------------------------------------------------
    # analyze execution
    # --------------------------------------------------------

    def analyze(
        self,
        side: str,
        order_price: float,
        fills: List[Dict],
        vwap: float,
        market_price: float,
        order_size: float,
        start_time: float,
        end_time: float,
        book_liquidity: float
    ) -> Dict:

        try:

            avg_fill = self._average_fill_price(fills)

            filled_size = sum(f["size"] for f in fills)

            slippage = self._slippage(
                side,
                order_price,
                avg_fill
            )

            vwap_dev = self._vwap_deviation(
                side,
                avg_fill,
                vwap
            )

            impact = self._market_impact(
                avg_fill,
                market_price
            )

            speed = self._fill_speed(
                start_time,
                end_time
            )

            liquidity = self._liquidity_usage(
                filled_size,
                book_liquidity
            )

            quality = self._quality_score(
                slippage,
                vwap_dev,
                impact,
                speed,
                liquidity
            )

            return {

                "avg_fill_price": avg_fill,

                "filled_size": filled_size,

                "slippage": slippage,

                "vwap_deviation": vwap_dev,

                "market_impact": impact,

                "execution_speed": speed,

                "liquidity_usage": liquidity,

                "execution_quality": quality

            }

        except Exception:

            logger.exception("Execution quality analysis failure")

            return {"execution_quality": 0}

    # --------------------------------------------------------
    # average fill
    # --------------------------------------------------------

    def _average_fill_price(self, fills):

        if not fills:
            return 0

        total = sum(f["price"] * f["size"] for f in fills)

        size = sum(f["size"] for f in fills)

        return _safe_div(total, size)

    # --------------------------------------------------------
    # slippage
    # --------------------------------------------------------

    def _slippage(self, side, order_price, fill_price):

        if order_price <= 0:
            return 0

        if side == "BUY":

            slip = (fill_price - order_price) / order_price

        else:

            slip = (order_price - fill_price) / order_price

        return slip

    # --------------------------------------------------------
    # vwap deviation
    # --------------------------------------------------------

    def _vwap_deviation(self, side, fill_price, vwap):

        if vwap <= 0:
            return 0

        if side == "BUY":

            dev = (fill_price - vwap) / vwap

        else:

            dev = (vwap - fill_price) / vwap

        return dev

    # --------------------------------------------------------
    # market impact
    # --------------------------------------------------------

    def _market_impact(self, fill_price, market_price):

        if market_price <= 0:
            return 0

        return abs(fill_price - market_price) / market_price

    # --------------------------------------------------------
    # fill speed
    # --------------------------------------------------------

    def _fill_speed(self, start, end):

        duration = max(end - start, 0.001)

        score = 1 / duration

        return _clip(score, 0, 1)

    # --------------------------------------------------------
    # liquidity usage
    # --------------------------------------------------------

    def _liquidity_usage(self, filled_size, liquidity):

        if liquidity <= 0:
            return 1

        return filled_size / liquidity

    # --------------------------------------------------------
    # quality score
    # --------------------------------------------------------

    def _quality_score(
        self,
        slippage,
        vwap_dev,
        impact,
        speed,
        liquidity
    ):

        score = 1 - (

            abs(slippage) * self.slippage_weight
            + abs(vwap_dev) * self.vwap_weight
            + impact * self.impact_weight
            + (1 - speed) * self.speed_weight
            + liquidity * self.liquidity_weight

        )

        return _clip(score)


# ============================================================
# Singleton
# ============================================================

_analyzer = None


def get_execution_quality_analyzer():

    global _analyzer

    if _analyzer is None:

        _analyzer = ExecutionQualityAnalyzer()

    return _analyzer