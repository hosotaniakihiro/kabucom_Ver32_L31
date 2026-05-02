# ============================================================
# trading/ai/ai_engine.py
# PRODUCTION AI ORCHESTRATOR (FULL VERSION)
#
# Integrates all AI modules
#
#   market regime layer
#   microstructure layer
#   flow layer
#   toxicity layer
#   execution layer
#   exit layer
#
# Designed for real-time trading systems
# ============================================================

from __future__ import annotations

import logging
import pandas as pd
from typing import Dict, Any

from trading.ai.microstructure.algo_detection_ai import get_algo_detector
from trading.ai.microstructure.iceberg_detector import get_iceberg_detector
from trading.ai.microstructure.spoof_detector import get_spoof_detector
from trading.ai.microstructure.orderflow_imbalance import get_orderflow_analyzer
from trading.ai.microstructure.toxicity_model import get_toxicity_model

from trading.ai.flow.institutional_flow_ai import get_institutional_flow_ai

from trading.ai.execution.vwap_execution_ai import get_vwap_execution_ai

from trading.ai.exit.tosama_inago_exit_ai import get_tosama_exit_ai

from trading.ai.market_regime_ai import get_market_regime_ai


logger = logging.getLogger(__name__)


# ============================================================
# AI Engine
# ============================================================

class AIEngine:

    def __init__(self):

        # microstructure
        self.algo_ai = get_algo_detector()
        self.iceberg_ai = get_iceberg_detector()
        self.spoof_ai = get_spoof_detector()
        self.orderflow_ai = get_orderflow_analyzer()
        self.toxicity_ai = get_toxicity_model()

        # flow
        self.flow_ai = get_institutional_flow_ai()

        # execution
        self.execution_ai = get_vwap_execution_ai()

        # exit
        self.exit_ai = get_tosama_exit_ai()

        # regime
        self.regime_ai = get_market_regime_ai()

    # ========================================================
    # Market Regime
    # ========================================================

    def detect_market_regime(
        self,
        df: pd.DataFrame,
        spread: float,
        bid_volume: float,
        ask_volume: float,
        algo_score: float,
        toxicity: float
    ) -> Dict[str, Any]:

        try:

            return self.regime_ai.detect(
                df,
                spread,
                bid_volume,
                ask_volume,
                algo_score,
                toxicity
            )

        except Exception:

            logger.exception("market regime detection failure")

            return {"regime": "UNKNOWN"}

    # ========================================================
    # Microstructure Analysis
    # ========================================================

    def analyze_microstructure(
        self,
        trades: pd.DataFrame,
        board_snapshot: Dict,
        order_events: Dict,
        bid_volume: float,
        ask_volume: float,
        board_updates: float,
        last_price: float
    ) -> Dict:

        try:

            iceberg_score = self.iceberg_ai.detect(
                trades,
                bid_volume,
                ask_volume,
                last_price
            )

            spoof_score = self.spoof_ai.detect(
                order_events,
                [],
                [],
                board_snapshot
            )

            flow = self.orderflow_ai.analyze(
                trades,
                board_snapshot
            )

            algo = self.algo_ai.detect(
                trades,
                bid_volume,
                ask_volume,
                order_events,
                board_updates
            )

            toxicity = self.toxicity_ai.compute(
                algo.get("algo_score", 0),
                spoof_score,
                iceberg_score,
                flow.get("orderflow_score", 0),
                algo.get("cancel_ratio", 0),
                algo.get("board_update_score", 0),
            )

            return {

                "iceberg": iceberg_score,
                "spoof": spoof_score,
                "orderflow": flow,
                "algo": algo,
                "toxicity": toxicity

            }

        except Exception:

            logger.exception("microstructure analysis failure")

            return {}

    # ========================================================
    # Institutional Flow
    # ========================================================

    def analyze_institutional_flow(
        self,
        trades: pd.DataFrame,
        vwap: float,
        bid_volume: float,
        ask_volume: float
    ) -> Dict:

        try:

            return self.flow_ai.analyze(
                trades,
                vwap,
                bid_volume,
                ask_volume
            )

        except Exception:

            logger.exception("institutional flow failure")

            return {}

    # ========================================================
    # Execution Optimization
    # ========================================================

    def optimize_execution(
        self,
        side: str,
        best_bid: float,
        best_ask: float,
        bid_size: float,
        ask_size: float,
        vwap: float,
        position_size: float,
        spread: float,
        toxicity: float
    ) -> Dict:

        try:

            return self.execution_ai.decide_execution(
                side,
                best_bid,
                best_ask,
                bid_size,
                ask_size,
                vwap,
                position_size,
                spread,
                toxicity
            )

        except Exception:

            logger.exception("execution optimization failure")

            return {}

    # ========================================================
    # Exit Decision
    # ========================================================

    def exit_signal(
        self,
        df: pd.DataFrame,
        bid_volume: float,
        ask_volume: float,
        spread: float,
        orderflow_score: float
    ) -> Dict:

        try:

            return self.exit_ai.analyze(
                df,
                bid_volume,
                ask_volume,
                spread,
                orderflow_score
            )

        except Exception:

            logger.exception("exit signal failure")

            return {"exit_signal": False}

    # ========================================================
    # Entry Filter
    # ========================================================

    def allow_entry(self, toxicity: Dict) -> bool:

        score = toxicity.get("toxicity", 0)

        if score > 0.8:
            return False

        return True

    # ========================================================
    # Full Market Analysis
    # ========================================================

    def full_market_analysis(
        self,
        df: pd.DataFrame,
        trades: pd.DataFrame,
        board_snapshot: Dict,
        order_events: Dict,
        bid_volume: float,
        ask_volume: float,
        board_updates: float,
        last_price: float,
        spread: float,
        vwap: float
    ) -> Dict:

        try:

            micro = self.analyze_microstructure(
                trades,
                board_snapshot,
                order_events,
                bid_volume,
                ask_volume,
                board_updates,
                last_price
            )

            regime = self.detect_market_regime(
                df,
                spread,
                bid_volume,
                ask_volume,
                micro.get("algo", {}).get("algo_score", 0),
                micro.get("toxicity", {}).get("toxicity", 0)
            )

            flow = self.analyze_institutional_flow(
                trades,
                vwap,
                bid_volume,
                ask_volume
            )

            return {

                "microstructure": micro,

                "regime": regime,

                "institutional_flow": flow

            }

        except Exception:

            logger.exception("full market analysis failure")

            return {}

    # ========================================================
    # Entry Decision
    # ========================================================

    def entry_decision(self, analysis: Dict) -> bool:

        try:

            toxicity = analysis.get(
                "microstructure", {}
            ).get(
                "toxicity", {}
            )

            regime = analysis.get(
                "regime", {}
            ).get(
                "regime", "UNKNOWN"
            )

            if not self.allow_entry(toxicity):

                return False

            if regime in ("ALGO_DOMINATED", "NEWS_SHOCK"):

                return False

            return True

        except Exception:

            logger.exception("entry decision failure")

            return False


# ============================================================
# Singleton
# ============================================================

_engine = None


def get_ai_engine() -> AIEngine:

    global _engine

    if _engine is None:

        _engine = AIEngine()

    return _engine