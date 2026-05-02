# ============================================================
# trading/ai/alpha_factory_ai.py
#
# AI ALPHA FACTORY
#
# Alpha generation and evaluation engine
#
# Responsibilities
#
#   feature combinations
#   alpha signal generation
#   correlation evaluation
#   alpha scoring
#
# ============================================================

from __future__ import annotations

import logging
import math
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
# Alpha Factory
# ============================================================

class AlphaFactoryAI:

    def __init__(self):

        self.generated_alphas: Dict[str, float] = {}

        self.alpha_history: List[Dict] = []

        self.max_history = 2000

    # --------------------------------------------------------
    # Generate alpha signals
    # --------------------------------------------------------

    def generate_alphas(self, df: pd.DataFrame) -> pd.DataFrame:

        try:

            if df is None or df.empty:

                return df

            df = df.copy()

            df["alpha_momentum"] = self._alpha_momentum(df)

            df["alpha_volume_flow"] = self._alpha_volume_flow(df)

            df["alpha_volatility_break"] = self._alpha_volatility_break(df)

            df["alpha_vwap_distance"] = self._alpha_vwap_distance(df)

            df["alpha_orderbook_pressure"] = self._alpha_orderbook_pressure(df)

            return df

        except Exception:

            logger.exception("Alpha generation failed")

            return df

    # --------------------------------------------------------
    # Momentum alpha
    # --------------------------------------------------------

    def _alpha_momentum(self, df):

        try:

            returns = df["close"].pct_change()

            alpha = returns.rolling(10).mean()

            return alpha.fillna(0)

        except Exception:

            return pd.Series([0]*len(df))

    # --------------------------------------------------------
    # Volume flow alpha
    # --------------------------------------------------------

    def _alpha_volume_flow(self, df):

        try:

            vol = df.get("volume")

            if vol is None:

                return pd.Series([0]*len(df))

            flow = vol.diff()

            alpha = flow.rolling(10).mean()

            return alpha.fillna(0)

        except Exception:

            return pd.Series([0]*len(df))

    # --------------------------------------------------------
    # Volatility breakout alpha
    # --------------------------------------------------------

    def _alpha_volatility_break(self, df):

        try:

            returns = df["close"].pct_change()

            vol = returns.rolling(20).std()

            breakout = returns.abs() > vol * 2

            return breakout.astype(float)

        except Exception:

            return pd.Series([0]*len(df))

    # --------------------------------------------------------
    # VWAP distance alpha
    # --------------------------------------------------------

    def _alpha_vwap_distance(self, df):

        try:

            price = df["close"]

            vwap = df.get("vwap")

            if vwap is None:

                return pd.Series([0]*len(df))

            alpha = (price - vwap) / vwap

            return alpha.fillna(0)

        except Exception:

            return pd.Series([0]*len(df))

    # --------------------------------------------------------
    # Orderbook pressure alpha
    # --------------------------------------------------------

    def _alpha_orderbook_pressure(self, df):

        try:

            bid = df.get("bid_volume")

            ask = df.get("ask_volume")

            if bid is None or ask is None:

                return pd.Series([0]*len(df))

            pressure = (bid - ask) / (bid + ask + 1e-9)

            return pressure.fillna(0)

        except Exception:

            return pd.Series([0]*len(df))

    # --------------------------------------------------------
    # Evaluate alpha correlation to returns
    # --------------------------------------------------------

    def evaluate_alphas(self, df: pd.DataFrame) -> Dict:

        try:

            if df is None or df.empty:

                return {}

            if "close" not in df:

                return {}

            future_returns = df["close"].pct_change().shift(-1)

            scores = {}

            for col in df.columns:

                if col.startswith("alpha_"):

                    try:

                        corr = df[col].corr(future_returns)

                        if math.isnan(corr):

                            corr = 0

                        scores[col] = float(corr)

                    except Exception:

                        scores[col] = 0

            self.generated_alphas = scores

            return scores

        except Exception:

            logger.exception("Alpha evaluation failed")

            return {}

    # --------------------------------------------------------
    # Select best alphas
    # --------------------------------------------------------

    def select_top_alphas(self, n=5):

        try:

            if not self.generated_alphas:

                return []

            items = sorted(

                self.generated_alphas.items(),

                key=lambda x: abs(x[1]),

                reverse=True

            )

            return items[:n]

        except Exception:

            return []

    # --------------------------------------------------------
    # Record alpha performance
    # --------------------------------------------------------

    def record_alpha(self, data: Dict):

        try:

            self.alpha_history.append(data)

            if len(self.alpha_history) > self.max_history:

                self.alpha_history.pop(0)

        except Exception:

            pass


# ============================================================
# Singleton
# ============================================================

_ai = None


def get_alpha_factory_ai():

    global _ai

    if _ai is None:

        _ai = AlphaFactoryAI()

    return _ai