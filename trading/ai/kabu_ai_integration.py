# ============================================================
# trading/ai/kabu_ai_integration.py
#
# Kabu System AI Integration Layer
#
# Connects existing kabu trading system with AI modules
#
# ============================================================

from __future__ import annotations

import logging
import pandas as pd
import threading
from typing import Dict, List

from trading.ai.ai_orchestrator import get_ai_orchestrator
from trading.ai.feature_store_ai import get_feature_store_ai
from trading.ai.execution_ai import get_execution_ai

logger = logging.getLogger(__name__)


# ============================================================
# Singleton
# ============================================================

_ai_bridge = None


def get_kabu_ai_bridge():

    global _ai_bridge

    if _ai_bridge is None:

        _ai_bridge = KabuAIIntegration()

    return _ai_bridge


# ============================================================
# Integration Class
# ============================================================

class KabuAIIntegration:

    def __init__(self):

        self.orchestrator = get_ai_orchestrator()

        self.feature_store = get_feature_store_ai()

        self.execution_ai = get_execution_ai()

        self.symbols: List[str] = []

        self.lock = threading.Lock()

    # --------------------------------------------------------
    # Register trading symbols
    # --------------------------------------------------------

    def set_symbols(self, symbols: List[str]):

        self.symbols = list(symbols)

        logger.info(f"[AI] symbols registered {symbols}")

    # --------------------------------------------------------
    # Tick ingestion
    # Called from push_stream
    # --------------------------------------------------------

    def ingest_tick(self, symbol: str, tick: Dict):

        try:

            self.feature_store.ingest_tick(symbol, tick)

        except Exception:

            logger.exception("tick ingest failed")

    # --------------------------------------------------------
    # Orderbook ingestion
    # --------------------------------------------------------

    def ingest_orderbook(self, symbol: str, book: Dict):

        try:

            self.feature_store.ingest_orderbook(symbol, book)

        except Exception:

            logger.exception("orderbook ingest failed")

    # --------------------------------------------------------
    # Summary ingestion
    # --------------------------------------------------------

    def ingest_summary(self, symbol: str, row: Dict):

        try:

            self.feature_store.ingest_summary(symbol, row)

        except Exception:

            logger.exception("summary ingest failed")

    # --------------------------------------------------------
    # Ranking ingestion
    # --------------------------------------------------------

    def ingest_ranking(self, symbol: str, ranking_row: Dict):

        try:

            self.feature_store.ingest_ranking(symbol, ranking_row)

        except Exception:

            logger.exception("ranking ingest failed")

    # --------------------------------------------------------
    # AI Decision Cycle
    # --------------------------------------------------------

    def run_cycle(self):

        try:

            for symbol in self.symbols:

                features = self.feature_store.build_features(symbol)

                if features is None:

                    continue

                decision = self.orchestrator.evaluate(

                    symbol,

                    features

                )

                self._execute(symbol, decision)

        except Exception:

            logger.exception("AI cycle failure")

    # --------------------------------------------------------
    # Execute trade
    # --------------------------------------------------------

    def _execute(self, symbol: str, decision: Dict):

        try:

            if decision is None:

                return

            action = decision.get("action")

            if action is None:

                return

            if action == "BUY":

                self.execution_ai.buy(

                    symbol,

                    decision

                )

            elif action == "SELL":

                self.execution_ai.sell(

                    symbol,

                    decision

                )

            elif action == "EXIT":

                self.execution_ai.exit(

                    symbol,

                    decision

                )

        except Exception:

            logger.exception("execution failure")

    # --------------------------------------------------------
    # External API for scheduler
    # --------------------------------------------------------

    def run_loop(self):

        logger.info("[AI] integration loop started")

        while True:

            self.run_cycle()