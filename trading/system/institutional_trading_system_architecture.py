# ============================================================
# institutional_trading_system_architecture.py
#
# Institutional Grade AI Trading System Architecture
#
# Integrates:
#
#   Alpha Discovery
#   Feature Store
#   Market Microstructure AI
#   Flow Detection
#   Price Analysis
#   Market Regime Detection
#   Decision Engines
#   Risk Management
#   Portfolio Allocation
#   Smart Execution
#   Self Learning
#   Backtesting
#   Market Simulation
#
# ============================================================

from __future__ import annotations

import logging
from typing import Dict

from trading.ai.alpha_factory_ai import get_alpha_factory_ai
from trading.ai.feature_store_ai import get_feature_store_ai

from trading.ai.algo_spike_ai import apply_algo_spike_ai
from trading.ai.orderbook_pressure_ai import apply_orderbook_pressure_ai
from trading.ai.institutional_flow_ai import get_institutional_flow_ai

from trading.ai.ranking_momentum_ai import apply_ranking_momentum_ai
from trading.ai.vwap_deviation_ai import apply_vwap_deviation_ai

from trading.ai.market_regime_ai import get_market_regime_ai

from trading.ai.meta_entry_ai import get_meta_entry_ai
from trading.ai.meta_exit_ai import get_meta_exit_ai

from trading.ai.risk_manager_ai import get_risk_manager_ai
from trading.ai.portfolio_ai import get_portfolio_ai
from trading.ai.execution_ai import get_execution_ai

from trading.ai.self_learning_ai import get_self_learning_ai
from trading.ai.backtest_engine_ai import get_backtest_engine_ai
from trading.ai.market_simulator_ai import get_market_simulator_ai

logger = logging.getLogger(__name__)


# ============================================================
# Institutional Trading System
# ============================================================

class InstitutionalTradingSystem:

    def __init__(self):

        self.alpha_factory = get_alpha_factory_ai()

        self.feature_store = get_feature_store_ai()

        self.regime_ai = get_market_regime_ai()

        self.entry_ai = get_meta_entry_ai()

        self.exit_ai = get_meta_exit_ai()

        self.risk_ai = get_risk_manager_ai()

        self.portfolio_ai = get_portfolio_ai()

        self.execution_ai = get_execution_ai()

        self.learning_ai = get_self_learning_ai()

        self.backtest_engine = get_backtest_engine_ai()

        self.market_simulator = get_market_simulator_ai()

        self.institutional_flow = get_institutional_flow_ai()

    # --------------------------------------------------------
    # Process market data
    # --------------------------------------------------------

    def process_market_data(self, df):

        try:

            df = apply_algo_spike_ai(df)

            df = apply_orderbook_pressure_ai(df)

            df = apply_ranking_momentum_ai(df)

            df = apply_vwap_deviation_ai(df)

            df = self.alpha_factory.generate_alphas(df)

            return df

        except Exception:

            logger.exception("Market processing failed")

            return df

    # --------------------------------------------------------
    # Feature update
    # --------------------------------------------------------

    def update_features(self, symbol: str, data: Dict):

        try:

            self.feature_store.update_features(symbol, data)

        except Exception:

            logger.exception("Feature update failed")

    # --------------------------------------------------------
    # Entry decision
    # --------------------------------------------------------

    def evaluate_entry(self, signals: Dict):

        try:

            regime = self.regime_ai.detect(

                df=signals.get("price_df"),

                spread=signals.get("spread"),

                bid_volume=signals.get("bid_volume"),

                ask_volume=signals.get("ask_volume"),

                algo_score=signals.get("algo_spike_score"),

                toxicity=signals.get("toxicity")

            )

            signals.update(regime)

            entry = self.entry_ai.evaluate(signals)

            risk = self.risk_ai.evaluate({

                **signals,
                **entry

            })

            return entry, risk

        except Exception:

            logger.exception("Entry evaluation failed")

            return {}, {}

    # --------------------------------------------------------
    # Exit decision
    # --------------------------------------------------------

    def evaluate_exit(self, signals: Dict):

        try:

            return self.exit_ai.evaluate(signals)

        except Exception:

            logger.exception("Exit evaluation failed")

            return {}

    # --------------------------------------------------------
    # Portfolio allocation
    # --------------------------------------------------------

    def allocate_portfolio(self, candidates):

        try:

            return self.portfolio_ai.allocate(candidates)

        except Exception:

            logger.exception("Portfolio allocation failed")

            return {}

    # --------------------------------------------------------
    # Execute order
    # --------------------------------------------------------

    def execute_order(self, symbol, signal):

        try:

            return self.execution_ai.decide_order(symbol, signal)

        except Exception:

            logger.exception("Execution failed")

            return {}

    # --------------------------------------------------------
    # Learning update
    # --------------------------------------------------------

    def record_trade(self, trade):

        try:

            self.learning_ai.record_trade(trade)

        except Exception:

            logger.exception("Learning update failed")

    # --------------------------------------------------------
    # Backtest
    # --------------------------------------------------------

    def run_backtest(self, df):

        try:

            return self.backtest_engine.run(df)

        except Exception:

            logger.exception("Backtest failed")

            return {}

    # --------------------------------------------------------
    # Market simulation
    # --------------------------------------------------------

    def simulate_market(self, steps=1000):

        try:

            return self.market_simulator.run(steps)

        except Exception:

            logger.exception("Simulation failed")

            return {}


# ============================================================
# Singleton
# ============================================================

_system = None


def get_institutional_trading_system():

    global _system

    if _system is None:

        _system = InstitutionalTradingSystem()

    return _system