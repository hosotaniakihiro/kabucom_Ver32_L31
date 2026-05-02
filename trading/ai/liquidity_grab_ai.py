# ============================================================
# trading/ai/liquidity_grab_ai.py
# PRODUCTION LIQUIDITY GRAB DETECTOR
#
# Detects:
#
#   stop hunt
#   liquidity grab
#   fake breakout
#   stop run
#   liquidity sweep
#
# Designed for microstructure trading
# ============================================================

from __future__ import annotations

import logging
import numpy as np
import pandas as pd
from typing import Dict

logger = logging.getLogger(__name__)


# ============================================================
# Utility
# ============================================================

def _clip(x, lo=0.0, hi=1.0):
    return max(lo, min(x, hi))


def _safe_div(a, b):
    try:
        if b == 0:
            return 0.0
        return float(a) / float(b)
    except Exception:
        return 0.0


# ============================================================
# Liquidity Grab AI
# ============================================================

class LiquidityGrabAI:

    def __init__(self):

        self.breakout_threshold = 0.003
        self.reversal_threshold = 0.002
        self.volume_spike_ratio = 2.5

    # --------------------------------------------------------
    # Main detection
    # --------------------------------------------------------

    def analyze(
        self,
        df: pd.DataFrame,
        volume: float,
        volume_avg: float,
        high: float,
        low: float,
        close: float
    ) -> Dict:

        try:

            if df is None or len(df) < 5:
                return self._no_signal()

            breakout = self._breakout(df, high, low)

            reversal = self._reversal(df)

            vol_spike = self._volume_spike(volume, volume_avg)

            sweep = self._sweep_detection(df)

            score_buy = (
                breakout["down_break"] * 0.3 +
                reversal["up_reversal"] * 0.3 +
                vol_spike * 0.2 +
                sweep["down_sweep"] * 0.2
            )

            score_sell = (
                breakout["up_break"] * 0.3 +
                reversal["down_reversal"] * 0.3 +
                vol_spike * 0.2 +
                sweep["up_sweep"] * 0.2
            )

            score_buy = _clip(score_buy)
            score_sell = _clip(score_sell)

            direction = self._direction(score_buy, score_sell)

            confidence = max(score_buy, score_sell)

            return {

                "liquidity_grab_buy": float(score_buy),

                "liquidity_grab_sell": float(score_sell),

                "direction": direction,

                "confidence": float(confidence),

                "components": {

                    "breakout": breakout,
                    "reversal": reversal,
                    "volume_spike": float(vol_spike),
                    "sweep": sweep

                }

            }

        except Exception:

            logger.exception("LiquidityGrabAI failure")

            return self._no_signal()

    # --------------------------------------------------------
    # Breakout detection
    # --------------------------------------------------------

    def _breakout(self, df, high, low):

        last_close = df["close"].iloc[-1]

        up_break = 0
        down_break = 0

        if last_close > high * (1 + self.breakout_threshold):
            up_break = 1

        if last_close < low * (1 - self.breakout_threshold):
            down_break = 1

        return {

            "up_break": up_break,
            "down_break": down_break

        }

    # --------------------------------------------------------
    # Reversal detection
    # --------------------------------------------------------

    def _reversal(self, df):

        if len(df) < 3:
            return {"up_reversal": 0, "down_reversal": 0}

        c1 = df["close"].iloc[-3]
        c2 = df["close"].iloc[-2]
        c3 = df["close"].iloc[-1]

        up_reversal = 0
        down_reversal = 0

        if c2 < c1 and c3 > c2 * (1 + self.reversal_threshold):
            up_reversal = 1

        if c2 > c1 and c3 < c2 * (1 - self.reversal_threshold):
            down_reversal = 1

        return {

            "up_reversal": up_reversal,
            "down_reversal": down_reversal

        }

    # --------------------------------------------------------
    # Volume spike
    # --------------------------------------------------------

    def _volume_spike(self, volume, volume_avg):

        if volume_avg == 0:
            return 0.0

        ratio = volume / volume_avg

        if ratio > self.volume_spike_ratio:
            return 1.0

        return ratio / self.volume_spike_ratio

    # --------------------------------------------------------
    # Sweep detection
    # --------------------------------------------------------

    def _sweep_detection(self, df):

        highs = df["high"]
        lows = df["low"]

        up_sweep = 0
        down_sweep = 0

        if highs.iloc[-1] > highs.iloc[-2] and df["close"].iloc[-1] < highs.iloc[-2]:
            up_sweep = 1

        if lows.iloc[-1] < lows.iloc[-2] and df["close"].iloc[-1] > lows.iloc[-2]:
            down_sweep = 1

        return {

            "up_sweep": up_sweep,
            "down_sweep": down_sweep

        }

    # --------------------------------------------------------
    # Direction
    # --------------------------------------------------------

    def _direction(self, buy, sell):

        if buy > sell * 1.2:
            return "LIQUIDITY_GRAB_LONG"

        if sell > buy * 1.2:
            return "LIQUIDITY_GRAB_SHORT"

        return "NEUTRAL"

    # --------------------------------------------------------
    # fallback
    # --------------------------------------------------------

    def _no_signal(self):

        return {

            "liquidity_grab_buy": 0.0,

            "liquidity_grab_sell": 0.0,

            "direction": "NONE",

            "confidence": 0.0,

            "components": {}

        }


# ============================================================
# Singleton
# ============================================================

_ai = None


def get_liquidity_grab_ai():

    global _ai

    if _ai is None:

        _ai = LiquidityGrabAI()

    return _ai