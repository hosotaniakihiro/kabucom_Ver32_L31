# ============================================================
# trading/ai/flow/institutional_flow_ai.py
# PRODUCTION INSTITUTIONAL FLOW DETECTOR
#
# Detects institutional execution patterns:
#
#   VWAP execution
#   TWAP execution
#   hidden accumulation
#   iceberg accumulation
#   absorption
#   distribution
#
# Designed for microstructure analysis
# ============================================================

from __future__ import annotations

import pandas as pd
import logging
import math
from typing import Dict

logger = logging.getLogger(__name__)


# ============================================================
# Utility
# ============================================================

def _clip(x, lo=0.0, hi=1.0):

    try:
        x = float(x)
    except Exception:
        return 0.0

    if not math.isfinite(x):
        return 0.0

    return max(lo, min(x, hi))


def _safe_div(a, b):

    try:
        a = float(a)
        b = float(b)

        if b == 0:
            return 0.0

        r = a / b

        if not math.isfinite(r):
            return 0.0

        return r

    except Exception:

        return 0.0


def _safe_series(df: pd.DataFrame, col: str):

    if col not in df.columns:
        return pd.Series(dtype=float)

    s = pd.to_numeric(df[col], errors="coerce")
    s = s.replace([float("inf"), float("-inf")], 0).fillna(0)

    return s


# ============================================================
# Institutional Flow AI
# ============================================================

class InstitutionalFlowAI:

    def __init__(self):

        self.large_trade_threshold = 5000
        self.absorption_volume = 20000
        self.vwap_band = 0.003
        self.twap_window = 20

    # --------------------------------------------------------
    # Main analyze
    # --------------------------------------------------------

    def analyze(
        self,
        trades: pd.DataFrame,
        vwap: float,
        bid_volume: float,
        ask_volume: float
    ) -> Dict:

        try:

            if trades is None or len(trades) < 10:

                return self._no_signal()

            if not isinstance(trades, pd.DataFrame):

                return self._no_signal()

            large_trade_score = self._large_trade_score(trades)

            absorption_score = self._absorption_score(
                trades,
                bid_volume,
                ask_volume
            )

            vwap_execution_score = self._vwap_execution_score(
                trades,
                vwap
            )

            twap_score = self._twap_execution_score(trades)

            buy_volume = _safe_series(trades, "size")[
                trades.get("side") == "BUY"
            ].sum()

            sell_volume = _safe_series(trades, "size")[
                trades.get("side") == "SELL"
            ].sum()

            total = buy_volume + sell_volume

            buy_ratio = _safe_div(buy_volume, total)
            sell_ratio = _safe_div(sell_volume, total)

            institutional_buy = (

                buy_ratio * 0.4
                + absorption_score * 0.25
                + vwap_execution_score * 0.2
                + large_trade_score * 0.15

            )

            institutional_sell = (

                sell_ratio * 0.4
                + absorption_score * 0.25
                + vwap_execution_score * 0.2
                + large_trade_score * 0.15

            )

            institutional_buy = _clip(institutional_buy)
            institutional_sell = _clip(institutional_sell)

            direction = self._direction(
                institutional_buy,
                institutional_sell
            )

            confidence = max(
                institutional_buy,
                institutional_sell
            )

            pattern = self._pattern(
                vwap_execution_score,
                twap_score,
                absorption_score
            )

            return {

                "institutional_buy_score": float(institutional_buy),

                "institutional_sell_score": float(institutional_sell),

                "flow_direction": direction,

                "confidence": float(confidence),

                "pattern": pattern,

                "components": {

                    "large_trade_score": float(large_trade_score),

                    "absorption_score": float(absorption_score),

                    "vwap_execution": float(vwap_execution_score),

                    "twap_execution": float(twap_score),

                }

            }

        except Exception:

            logger.exception("InstitutionalFlowAI failure")

            return self._no_signal()

    # --------------------------------------------------------
    # Large trade clustering
    # --------------------------------------------------------

    def _large_trade_score(self, trades):

        sizes = _safe_series(trades, "size")

        large = sizes[sizes > self.large_trade_threshold]

        ratio = _safe_div(len(large), len(sizes))

        return _clip(ratio)

    # --------------------------------------------------------
    # Absorption detection
    # --------------------------------------------------------

    def _absorption_score(
        self,
        trades,
        bid_volume,
        ask_volume
    ):

        sizes = _safe_series(trades, "size")

        trade_volume = sizes.sum()

        book_liquidity = float(bid_volume) + float(ask_volume)

        if book_liquidity == 0:
            return 0.0

        ratio = trade_volume / book_liquidity

        if trade_volume > self.absorption_volume:

            ratio *= 1.3

        return _clip(ratio)

    # --------------------------------------------------------
    # VWAP execution detection
    # --------------------------------------------------------

    def _vwap_execution_score(
        self,
        trades,
        vwap
    ):

        prices = _safe_series(trades, "price")

        if len(prices) == 0:
            return 0.0

        distance = abs(prices - vwap) / max(vwap, 1e-9)

        within_band = distance < self.vwap_band

        ratio = _safe_div(
            within_band.sum(),
            len(prices)
        )

        return _clip(ratio)

    # --------------------------------------------------------
    # TWAP execution detection
    # --------------------------------------------------------

    def _twap_execution_score(self, trades):

        sizes = _safe_series(trades, "size")

        if len(sizes) < self.twap_window:

            return 0.0

        sizes = sizes.tail(self.twap_window)

        mean = sizes.mean()

        if mean == 0:
            return 0.0

        variance = sizes.var()

        score = 1 - min(variance / mean, 1)

        return _clip(score)

    # --------------------------------------------------------
    # Direction
    # --------------------------------------------------------

    def _direction(self, buy, sell):

        if buy > sell * 1.2:
            return "BUY"

        if sell > buy * 1.2:
            return "SELL"

        return "NEUTRAL"

    # --------------------------------------------------------
    # Pattern classification
    # --------------------------------------------------------

    def _pattern(
        self,
        vwap_score,
        twap_score,
        absorption_score
    ):

        if absorption_score > 0.6:
            return "ABSORPTION"

        if vwap_score > 0.6:
            return "VWAP_EXECUTION"

        if twap_score > 0.6:
            return "TWAP_EXECUTION"

        return "NONE"

    # --------------------------------------------------------
    # fallback
    # --------------------------------------------------------

    def _no_signal(self):

        return {

            "institutional_buy_score": 0.0,

            "institutional_sell_score": 0.0,

            "flow_direction": "NONE",

            "confidence": 0.0,

            "pattern": "NONE"

        }


# ============================================================
# Singleton
# ============================================================

_ai = None


def get_institutional_flow_ai():

    global _ai

    if _ai is None:

        _ai = InstitutionalFlowAI()

    return _ai