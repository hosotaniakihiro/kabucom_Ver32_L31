# ============================================================
# trading/ai/self_learning_ai.py
#
# AI SELF LEARNING ENGINE
#
# Responsibilities
#
#   trade result logging
#   performance statistics
#   feature importance estimation
#   adaptive weight learning
#
# ============================================================

from __future__ import annotations

import logging
import math
import time
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


def _safe_mean(x):

    try:

        if len(x) == 0:
            return 0.0

        return float(np.mean(x))

    except Exception:
        return 0.0


# ============================================================
# Self Learning AI
# ============================================================

class SelfLearningAI:

    def __init__(self):

        self.trade_history: List[Dict] = []

        self.feature_scores: Dict[str, float] = {}

        self.max_history = 5000

    # --------------------------------------------------------
    # Record trade result
    # --------------------------------------------------------

    def record_trade(self, trade: Dict):

        try:

            trade["timestamp"] = time.time()

            self.trade_history.append(trade)

            if len(self.trade_history) > self.max_history:

                self.trade_history.pop(0)

        except Exception:

            logger.exception("Trade recording failed")

    # --------------------------------------------------------
    # Compute performance statistics
    # --------------------------------------------------------

    def compute_statistics(self) -> Dict:

        try:

            if not self.trade_history:

                return {}

            df = pd.DataFrame(self.trade_history)

            if "pnl" not in df.columns:

                return {}

            wins = df[df["pnl"] > 0]

            losses = df[df["pnl"] <= 0]

            win_rate = len(wins) / max(len(df), 1)

            avg_win = _safe_mean(wins["pnl"].values)

            avg_loss = _safe_mean(losses["pnl"].values)

            expectancy = win_rate * avg_win + (1 - win_rate) * avg_loss

            return {

                "trades": len(df),

                "win_rate": win_rate,

                "avg_win": avg_win,

                "avg_loss": avg_loss,

                "expectancy": expectancy

            }

        except Exception:

            logger.exception("Statistics computation failed")

            return {}

    # --------------------------------------------------------
    # Feature importance estimation
    # --------------------------------------------------------

    def compute_feature_importance(self) -> Dict:

        try:

            if not self.trade_history:

                return {}

            df = pd.DataFrame(self.trade_history)

            if "pnl" not in df.columns:

                return {}

            numeric_cols = df.select_dtypes(include=[np.number]).columns

            scores = {}

            for col in numeric_cols:

                if col == "pnl":

                    continue

                try:

                    corr = df[col].corr(df["pnl"])

                    if math.isnan(corr):

                        corr = 0

                    scores[col] = float(corr)

                except Exception:

                    scores[col] = 0

            self.feature_scores = scores

            return scores

        except Exception:

            logger.exception("Feature importance failed")

            return {}

    # --------------------------------------------------------
    # Adaptive weight update
    # --------------------------------------------------------

    def adjust_weights(self, weights: Dict) -> Dict:

        try:

            if not self.feature_scores:

                return weights

            adjusted = {}

            for k, w in weights.items():

                importance = self.feature_scores.get(k, 0)

                factor = 1 + importance

                adjusted[k] = w * factor

            total = sum(adjusted.values())

            if total == 0:

                return weights

            return {

                k: v / total

                for k, v in adjusted.items()

            }

        except Exception:

            logger.exception("Weight adjustment failed")

            return weights

    # --------------------------------------------------------
    # Get trade history
    # --------------------------------------------------------

    def get_history(self):

        try:

            return pd.DataFrame(self.trade_history)

        except Exception:

            return pd.DataFrame()


# ============================================================
# Singleton
# ============================================================

_ai = None


def get_self_learning_ai():

    global _ai

    if _ai is None:

        _ai = SelfLearningAI()

    return _ai