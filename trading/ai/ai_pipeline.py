# ============================================================
# trading/ai/ai_pipeline.py
# PRODUCTION AI PIPELINE
#
# Runs complete AI stack
#
#   regime
#   microstructure
#   flow
#   alpha
#   position sizing
#   portfolio risk
#   execution
#   exit
#
# Single entry point for AI decision
# ============================================================

from __future__ import annotations

import logging
from typing import Dict

from trading.ai.ai_engine import get_ai_engine
from trading.ai.alpha_signal_ai import get_alpha_signal_ai
from trading.ai.adaptive_position_ai import get_adaptive_position_ai
from trading.ai.portfolio_risk_ai import get_portfolio_risk_ai


logger = logging.getLogger(__name__)


# ============================================================
# AI Pipeline
# ============================================================

class AIPipeline:

    def __init__(self):

        self.engine = get_ai_engine()

        self.alpha_ai = get_alpha_signal_ai()

        self.position_ai = get_adaptive_position_ai()

        self.portfolio_ai = get_portfolio_risk_ai()

    # --------------------------------------------------------
    # Run full AI pipeline
    # --------------------------------------------------------

    def run(
        self,
        symbol: str,
        df,
        trades,
        board_snapshot,
        order_events,
        bid_volume,
        ask_volume,
        board_updates,
        last_price,
        spread,
        vwap,
        score,
        momentum,
        capital,
        portfolio_positions,
        correlation_matrix,
        sector_map,
        best_bid,
        best_ask,
        bid_size,
        ask_size
    ) -> Dict:

        try:

            # =================================================
            # AI ENGINE
            # =================================================

            analysis = self.engine.full_market_analysis(
                df,
                trades,
                board_snapshot,
                order_events,
                bid_volume,
                ask_volume,
                board_updates,
                last_price,
                spread,
                vwap
            )

            # =================================================
            # ALPHA SIGNAL
            # =================================================

            alpha = self.alpha_ai.evaluate(
                analysis,
                score,
                momentum
            )

            if not alpha.get("allow_trade"):

                return {
                    "allow_trade": False,
                    "reason": "alpha_reject"
                }

            # =================================================
            # POSITION SIZE
            # =================================================

            regime = analysis.get("regime", {})

            volatility = regime.get("volatility", 0)

            liquidity = bid_volume + ask_volume

            position = self.position_ai.size_position(
                alpha,
                volatility,
                liquidity,
                regime,
                capital
            )

            # =================================================
            # PORTFOLIO RISK
            # =================================================

            portfolio = self.portfolio_ai.evaluate(
                symbol,
                position["position_value"],
                capital,
                portfolio_positions,
                correlation_matrix,
                sector_map
            )

            if not portfolio.get("allow_trade"):

                return {
                    "allow_trade": False,
                    "reason": "portfolio_risk"
                }

            position_value = portfolio["adjusted_position_value"]

            # =================================================
            # EXECUTION OPTIMIZATION
            # =================================================

            toxicity = analysis.get(
                "microstructure", {}
            ).get(
                "toxicity", {}
            ).get(
                "toxicity", 0
            )

            execution = self.engine.optimize_execution(
                "BUY",
                best_bid,
                best_ask,
                bid_size,
                ask_size,
                vwap,
                position_value,
                spread,
                toxicity
            )

            # =================================================
            # EXIT SIGNAL
            # =================================================

            exit_signal = self.engine.exit_signal(
                df,
                bid_volume,
                ask_volume,
                spread,
                analysis.get(
                    "microstructure", {}
                ).get(
                    "orderflow", {}
                ).get(
                    "orderflow_score", 0
                )
            )

            return {

                "allow_trade": True,

                "analysis": analysis,

                "alpha": alpha,

                "position": position,

                "portfolio": portfolio,

                "execution": execution,

                "exit": exit_signal

            }

        except Exception:

            logger.exception("AI pipeline failure")

            return {
                "allow_trade": False,
                "reason": "pipeline_error"
            }


# ============================================================
# Singleton
# ============================================================

_pipeline = None


def get_ai_pipeline():

    global _pipeline

    if _pipeline is None:

        _pipeline = AIPipeline()

    return _pipeline