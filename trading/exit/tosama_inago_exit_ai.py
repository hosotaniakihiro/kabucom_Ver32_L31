# ============================================================
# trading/ai/exit/tosama_inago_exit_ai.py
# PRODUCTION TOSAMA INAGO EXIT AI
#
# Detects collapse of momentum-chasing retail flows
#
# Uses:
#   VWAP deviation
#   volume spike
#   wick ratio
#   orderflow reversal
#   liquidity collapse
#   spread expansion
#
# Output:
#   exit signal
#   exit probability
# ============================================================

from __future__ import annotations

import numpy as np
import pandas as pd
import logging
from typing import Dict

logger = logging.getLogger(__name__)


# ============================================================
# Utility
# ============================================================

def _clip(x, lo=0.0, hi=1.0):
    return max(lo, min(x, hi))


def _safe_div(a, b):

    if b == 0:
        return 0.0

    return float(a) / float(b)


# ============================================================
# Tosama Inago Exit AI
# ============================================================

class TosamaInagoExitAI:

    def __init__(self):

        self.vwap_dev_threshold = 0.02

        self.volume_spike_ratio = 3.0

        self.wick_threshold = 0.35

        self.spread_threshold = 0.004

        self.momentum_window = 6

    # --------------------------------------------------------
    # Main analysis
    # --------------------------------------------------------

    def analyze(
        self,
        df: pd.DataFrame,
        bid_volume: float,
        ask_volume: float,
        spread: float,
        orderflow_score: float,
    ) -> Dict:

        try:

            if df is None or len(df) < 10:

                return self._no_exit()

            last = df.iloc[-1]

            price = last["close"]

            vwap = last.get("vwap", price)

            volume = last.get("volume", 0)

            avg_vol = df["volume"].rolling(20).mean().iloc[-1]

            vwap_dev = abs(price - vwap) / max(vwap, 1e-9)

            volume_spike = _safe_div(volume, avg_vol)

            wick = self._wick_ratio(last)

            flow_reversal = self._orderflow_reversal(orderflow_score)

            liquidity_collapse = self._liquidity_collapse(
                bid_volume,
                ask_volume
            )

            momentum_loss = self._momentum_slowdown(df)

            spread_risk = _clip(spread / self.spread_threshold)

            exit_score = (

                vwap_dev * 0.25
                + volume_spike * 0.20
                + wick * 0.15
                + flow_reversal * 0.15
                + liquidity_collapse * 0.15
                + momentum_loss * 0.05
                + spread_risk * 0.05

            )

            exit_score = _clip(exit_score)

            signal = exit_score > 0.65

            urgency = self._urgency(exit_score)

            return {

                "exit_signal": bool(signal),

                "exit_probability": float(exit_score),

                "urgency": urgency,

                "reason": "tosama_inago",

                "components": {

                    "vwap_dev": float(vwap_dev),

                    "volume_spike": float(volume_spike),

                    "wick_ratio": float(wick),

                    "flow_reversal": float(flow_reversal),

                    "liquidity_collapse": float(liquidity_collapse),

                    "momentum_loss": float(momentum_loss),

                    "spread_risk": float(spread_risk),

                }

            }

        except Exception:

            logger.exception("TosamaInagoExitAI failure")

            return self._no_exit()

    # --------------------------------------------------------
    # Wick ratio
    # --------------------------------------------------------

    def _wick_ratio(self, row):

        high = row["high"]
        low = row["low"]
        close = row["close"]
        open_ = row["open"]

        body = abs(close - open_)
        full = high - low

        if full == 0:
            return 0.0

        upper_wick = high - max(open_, close)

        return _clip(upper_wick / full)

    # --------------------------------------------------------
    # Orderflow reversal
    # --------------------------------------------------------

    def _orderflow_reversal(self, orderflow_score):

        return _clip(-orderflow_score)

    # --------------------------------------------------------
    # Liquidity collapse
    # --------------------------------------------------------

    def _liquidity_collapse(
        self,
        bid_volume,
        ask_volume
    ):

        total = bid_volume + ask_volume

        if total == 0:
            return 1.0

        imbalance = abs(bid_volume - ask_volume) / total

        return _clip(imbalance)

    # --------------------------------------------------------
    # Momentum slowdown
    # --------------------------------------------------------

    def _momentum_slowdown(self, df):

        recent = df.tail(self.momentum_window)

        prices = recent["close"].values

        if len(prices) < 3:
            return 0.0

        slope = np.polyfit(range(len(prices)), prices, 1)[0]

        norm = abs(prices[-1]) + 1e-9

        momentum = slope / norm

        if momentum < 0:

            return _clip(abs(momentum) * 10)

        return 0.0

    # --------------------------------------------------------
    # urgency
    # --------------------------------------------------------

    def _urgency(self, score):

        if score > 0.85:
            return "IMMEDIATE"

        if score > 0.7:
            return "HIGH"

        if score > 0.55:
            return "MEDIUM"

        return "LOW"

    # --------------------------------------------------------
    # fallback
    # --------------------------------------------------------

    def _no_exit(self):

        return {

            "exit_signal": False,

            "exit_probability": 0.0,

            "urgency": "LOW",

            "reason": "none"

        }


# ============================================================
# Singleton
# ============================================================

_ai = None


def get_tosama_exit_ai():

    global _ai

    if _ai is None:

        _ai = TosamaInagoExitAI()

    return _ai