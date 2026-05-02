# ============================================================
# trading/ai/market_regime_ai.py
# PRODUCTION MARKET REGIME DETECTOR (FULL VERSION)
#
# Detects market conditions:
#
#   TREND_UP
#   TREND_DOWN
#   RANGE
#   VOLATILE
#   ALGO_DOMINATED
#   NEWS_SHOCK
#   LOW_LIQUIDITY
#
# Used for strategy switching
# ============================================================

from __future__ import annotations

import numpy as np
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


def _safe_std(x):

    try:

        v = np.std(x)

        if np.isnan(v) or np.isinf(v):
            return 0.0

        return float(v)

    except Exception:

        return 0.0


def _safe_polyfit(x, y):

    try:

        slope = np.polyfit(x, y, 1)[0]

        if np.isnan(slope) or np.isinf(slope):
            return 0.0

        return float(slope)

    except Exception:

        return 0.0


def _get_price_column(df: pd.DataFrame):

    if "close" in df.columns:
        return "close"

    if "close_price" in df.columns:
        return "close_price"

    return None


# ============================================================
# Market Regime AI
# ============================================================

class MarketRegimeAI:

    def __init__(self):

        self.trend_window = 30
        self.vol_window = 20

        self.low_liquidity_threshold = 10000
        self.news_move_threshold = 0.03

        self.volatility_threshold = 0.02
        self.trend_threshold = 0.002

    # --------------------------------------------------------
    # Main detection
    # --------------------------------------------------------

    def detect(
        self,
        df: pd.DataFrame,
        spread: float,
        bid_volume: float,
        ask_volume: float,
        algo_score: float,
        toxicity: float
    ) -> Dict:

        try:

            trend = self._trend(df)

            volatility = self._volatility(df)

            liquidity = float(bid_volume) + float(ask_volume)

            news_shock = self._news_shock(df)

            regime = self._classify(
                trend,
                volatility,
                liquidity,
                news_shock,
                algo_score,
                toxicity
            )

            confidence = self._confidence(
                trend,
                volatility,
                liquidity,
                algo_score,
                toxicity
            )

            regime_score = self._regime_score(regime)

            return {

                "regime": regime,

                "regime_score": float(regime_score),

                "confidence": float(confidence),

                "trend_strength": float(trend),

                "volatility": float(volatility),

                "liquidity": float(liquidity),

                "spread": float(spread),

                "news_shock": bool(news_shock)

            }

        except Exception:

            logger.exception("MarketRegimeAI failure")

            return {
                "regime": "UNKNOWN",
                "confidence": 0.0
            }

    # --------------------------------------------------------
    # Trend detection
    # --------------------------------------------------------

    def _trend(self, df):

        if df is None or len(df) < self.trend_window:
            return 0.0

        try:

            col = _get_price_column(df)

            if col is None:
                return 0.0

            prices = pd.to_numeric(
                df[col].tail(self.trend_window),
                errors="coerce"
            ).dropna()

            if len(prices) < self.trend_window / 2:
                return 0.0

            prices = prices.values

            x = np.arange(len(prices))

            slope = _safe_polyfit(x, prices)

            norm = abs(prices[-1]) + 1e-9

            trend = slope / norm

            return float(trend)

        except Exception:

            return 0.0

    # --------------------------------------------------------
    # Volatility
    # --------------------------------------------------------

    def _volatility(self, df):

        if df is None or len(df) < self.vol_window:
            return 0.0

        try:

            col = _get_price_column(df)

            if col is None:
                return 0.0

            prices = pd.to_numeric(df[col], errors="coerce")

            returns = prices.pct_change().dropna()

            vol = _safe_std(
                returns.tail(self.vol_window).values
            )

            return float(vol)

        except Exception:

            return 0.0

    # --------------------------------------------------------
    # News shock
    # --------------------------------------------------------

    def _news_shock(self, df):

        if df is None or len(df) < 3:
            return False

        try:

            col = _get_price_column(df)

            if col is None:
                return False

            prev = float(df[col].iloc[-2])
            last = float(df[col].iloc[-1])

            move = abs(last - prev) / max(prev, 1e-9)

            return bool(move > self.news_move_threshold)

        except Exception:

            return False

    # --------------------------------------------------------
    # Regime classification
    # --------------------------------------------------------

    def _classify(
        self,
        trend,
        volatility,
        liquidity,
        news_shock,
        algo_score,
        toxicity
    ):

        if news_shock:

            return "NEWS_SHOCK"

        if liquidity < self.low_liquidity_threshold:

            return "LOW_LIQUIDITY"

        if toxicity > 0.8 or algo_score > 0.8:

            return "ALGO_DOMINATED"

        if volatility > self.volatility_threshold:

            return "VOLATILE"

        if trend > self.trend_threshold:

            return "TREND_UP"

        if trend < -self.trend_threshold:

            return "TREND_DOWN"

        return "RANGE"

    # --------------------------------------------------------
    # Confidence
    # --------------------------------------------------------

    def _confidence(
        self,
        trend,
        volatility,
        liquidity,
        algo_score,
        toxicity
    ):

        try:

            trend_score = abs(trend) * 50

            vol_score = volatility * 10

            liq_score = liquidity / 100000

            algo_penalty = algo_score * 0.5

            tox_penalty = toxicity * 0.5

            score = (

                trend_score
                + vol_score
                + liq_score
                - algo_penalty
                - tox_penalty

            )

            return _clip(score)

        except Exception:

            return 0.0

    # --------------------------------------------------------
    # Regime score
    # --------------------------------------------------------

    def _regime_score(self, regime):

        table = {

            "TREND_UP": 1.0,

            "TREND_DOWN": 0.9,

            "RANGE": 0.5,

            "VOLATILE": 0.4,

            "ALGO_DOMINATED": 0.2,

            "LOW_LIQUIDITY": 0.1,

            "NEWS_SHOCK": 0.0

        }

        return table.get(regime, 0.3)


# ============================================================
# Singleton
# ============================================================

_ai = None


def get_market_regime_ai():

    global _ai

    if _ai is None:

        _ai = MarketRegimeAI()

    return _ai