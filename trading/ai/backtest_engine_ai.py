# ============================================================
# trading/ai/backtest_engine_ai.py
#
# AI BACKTEST ENGINE
#
# Backtests AI trading strategies
#
# Computes
#
#   PnL
#   win rate
#   Sharpe ratio
#   profit factor
#   max drawdown
#
# ============================================================

from __future__ import annotations

import logging
import math
from typing import Dict, List

import numpy as np
import pandas as pd

from trading.ai.ai_orchestrator import get_ai_orchestrator

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


def _max_drawdown(equity_curve):

    peak = equity_curve[0]

    max_dd = 0

    for x in equity_curve:

        if x > peak:

            peak = x

        dd = (peak - x)

        if dd > max_dd:

            max_dd = dd

    return max_dd


# ============================================================
# Backtest Engine
# ============================================================

class BacktestEngineAI:

    def __init__(self):

        self.orchestrator = get_ai_orchestrator()

        self.initial_capital = 1_000_000

    # --------------------------------------------------------
    # Run backtest
    # --------------------------------------------------------

    def run(self, df: pd.DataFrame) -> Dict:

        try:

            capital = self.initial_capital

            position = None

            equity_curve = []

            trades = []

            for i in range(len(df)):

                row = df.iloc[i]

                price = _safe(row.get("close"))

                signals = {

                    "price": price,

                    "spread": _safe(row.get("spread")),

                    "bid_volume": _safe(row.get("bid_volume")),

                    "ask_volume": _safe(row.get("ask_volume")),

                    "algo_spike_score": _safe(
                        row.get("algo_spike_score")
                    ),

                    "toxicity": _safe(
                        row.get("toxicity")
                    ),

                    "price_df": df.iloc[: i + 1]

                }

                enter, result = self.orchestrator.should_enter(
                    signals
                )

                exit_, exit_result = self.orchestrator.should_exit(
                    signals
                )

                if position is None and enter:

                    size = result["risk"].get(
                        "position_size", 0
                    )

                    if size > 0:

                        qty = capital * size / price

                        position = {

                            "entry_price": price,

                            "qty": qty

                        }

                elif position is not None and exit_:

                    pnl = (

                        price
                        - position["entry_price"]

                    ) * position["qty"]

                    capital += pnl

                    trades.append(pnl)

                    position = None

                equity_curve.append(capital)

            return self._statistics(
                equity_curve,
                trades
            )

        except Exception:

            logger.exception("Backtest failed")

            return {}

    # --------------------------------------------------------
    # Statistics
    # --------------------------------------------------------

    def _statistics(
        self,
        equity_curve: List[float],
        trades: List[float]
    ) -> Dict:

        try:

            if not trades:

                return {}

            wins = [t for t in trades if t > 0]

            losses = [t for t in trades if t <= 0]

            win_rate = len(wins) / len(trades)

            avg_win = np.mean(wins) if wins else 0

            avg_loss = np.mean(losses) if losses else 0

            profit_factor = (

                sum(wins) / abs(sum(losses))

                if losses else 0

            )

            returns = np.diff(equity_curve)

            sharpe = (

                np.mean(returns)

                / (np.std(returns) + 1e-9)

            )

            max_dd = _max_drawdown(equity_curve)

            return {

                "trades": len(trades),

                "win_rate": float(win_rate),

                "avg_win": float(avg_win),

                "avg_loss": float(avg_loss),

                "profit_factor": float(profit_factor),

                "sharpe": float(sharpe),

                "max_drawdown": float(max_dd),

                "final_equity": float(
                    equity_curve[-1]
                )

            }

        except Exception:

            logger.exception("Statistics error")

            return {}


# ============================================================
# Singleton
# ============================================================

_ai = None


def get_backtest_engine_ai():

    global _ai

    if _ai is None:

        _ai = BacktestEngineAI()

    return _ai