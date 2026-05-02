# ============================================================
# trading/ai/ai_trading_orchestrator.py
#
# PRODUCTION AI TRADING ORCHESTRATOR
#
# Central brain of AI trading system
#
# Coordinates:
#
#   feature_engine
#   feature_store
#   normalizer
#   alpha_model
#   RL agent
#   position sizing
#   risk control
#
# ============================================================

from __future__ import annotations

import logging
from typing import Dict

from trading.ai.microstructure.microstructure_feature_engine import (
    get_microstructure_feature_engine
)

from trading.ai.feature_store import get_feature_store
from trading.ai.online_feature_normalizer import get_online_feature_normalizer

from trading.ai.alpha_model import get_alpha_model
from trading.ai.rl_execution_agent import get_rl_execution_agent

from trading.ai.adaptive_position_ai import get_adaptive_position_ai
from trading.ai.portfolio_risk_ai import get_portfolio_risk_ai
from trading.ai.risk_engine import get_risk_engine

logger = logging.getLogger(__name__)


# ============================================================
# Orchestrator
# ============================================================

class AITradingOrchestrator:

    def __init__(self):

        self.feature_engine = get_microstructure_feature_engine()

        self.feature_store = get_feature_store()

        self.normalizer = get_online_feature_normalizer()

        self.alpha_model = get_alpha_model()

        self.rl_agent = get_rl_execution_agent()

        self.position_ai = get_adaptive_position_ai()

        self.portfolio_ai = get_portfolio_risk_ai()

        self.risk_engine = get_risk_engine()

    # --------------------------------------------------------
    # main pipeline
    # --------------------------------------------------------

    def process_tick(
        self,
        symbol: str,
        timestamp,
        orderbook: Dict,
        trades,
        prices,
        portfolio_state: Dict
    ) -> Dict:

        try:

            # -----------------------------------------------
            # 1 feature extraction
            # -----------------------------------------------

            features = self.feature_engine.compute(

                orderbook,
                trades,
                prices

            )

            # -----------------------------------------------
            # 2 store features
            # -----------------------------------------------

            self.feature_store.insert(

                symbol,
                timestamp,
                features

            )

            # -----------------------------------------------
            # 3 normalize
            # -----------------------------------------------

            features_norm = self.normalizer.normalize(features)

            # -----------------------------------------------
            # 4 alpha prediction
            # -----------------------------------------------

            alpha = self.alpha_model.predict(

                features_norm

            )

            # -----------------------------------------------
            # 5 RL decision
            # -----------------------------------------------

            decision = self.rl_agent.act(

                features_norm

            )

            action = decision.get("action")

            confidence = decision.get("confidence")

            # -----------------------------------------------
            # 6 position sizing
            # -----------------------------------------------

            position = self.position_ai.compute_size(

                alpha_score=alpha["alpha_score"],

                confidence=confidence

            )

            # -----------------------------------------------
            # 7 portfolio risk
            # -----------------------------------------------

            portfolio_risk = self.portfolio_ai.evaluate(

                portfolio_state

            )

            # -----------------------------------------------
            # 8 risk control
            # -----------------------------------------------

            risk = self.risk_engine.evaluate_trade(

                symbol=symbol,

                action=action,

                position_size=position,

                portfolio_risk=portfolio_risk.get("risk", 0),

                volatility=features.get("micro_volatility", 0),

                drawdown=portfolio_state.get("drawdown", 0),

                daily_pnl=portfolio_state.get("daily_pnl", 0),

                correlation=portfolio_state.get("correlation", 0)

            )

            # -----------------------------------------------
            # blocked
            # -----------------------------------------------

            if not risk.get("allowed"):

                return {

                    "symbol": symbol,

                    "action": "BLOCK",

                    "reason": risk.get("reason")

                }

            # -----------------------------------------------
            # final order
            # -----------------------------------------------

            return {

                "symbol": symbol,

                "action": action,

                "size": risk.get("adjusted_size"),

                "alpha": alpha,

                "confidence": confidence

            }

        except Exception:

            logger.exception("AI orchestrator failure")

            return {

                "symbol": symbol,

                "action": "ERROR"

            }

    # --------------------------------------------------------
    # health check
    # --------------------------------------------------------

    def status(self):

        return {

            "feature_engine": True,

            "feature_store_symbols": len(

                self.feature_store.symbols()

            ),

            "alpha_model_loaded": self.alpha_model.loaded,

            "rl_agent_loaded": self.rl_agent.loaded

        }


# ============================================================
# Singleton
# ============================================================

_orchestrator = None


def get_ai_trading_orchestrator():

    global _orchestrator

    if _orchestrator is None:

        _orchestrator = AITradingOrchestrator()

    return _orchestrator