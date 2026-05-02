# ============================================================
# ultimate_ai_trading_engine.py
#
# ULTIMATE AI TRADING ENGINE
#
# Integrates full AI trading architecture
#
# ============================================================

from __future__ import annotations

import logging
import time
from typing import List, Dict

from trading.data.data_pipeline_ai import get_data_pipeline_ai
from trading.system.institutional_trading_system_architecture import (
    get_institutional_trading_system
)

from trading.system.scheduler_ai import get_scheduler_ai
from trading.system.latency_monitor_ai import get_latency_monitor_ai
from trading.system.risk_guardian_ai import get_risk_guardian_ai

from trading.ai.execution_ai import get_execution_ai

logger = logging.getLogger(__name__)


# ============================================================
# Ultimate AI Trading Engine
# ============================================================

class UltimateAITradingEngine:

    def __init__(self):

        self.pipeline = get_data_pipeline_ai()

        self.system = get_institutional_trading_system()

        self.scheduler = get_scheduler_ai()

        self.latency = get_latency_monitor_ai()

        self.risk_guardian = get_risk_guardian_ai()

        self.execution = get_execution_ai()

        self.symbols: List[str] = []

        self.running = False

    # --------------------------------------------------------
    # Set trading universe
    # --------------------------------------------------------

    def set_symbols(self, symbols: List[str]):

        self.symbols = symbols

        self.scheduler.set_symbols(symbols)

    # --------------------------------------------------------
    # Process single symbol
    # --------------------------------------------------------

    def process_symbol(self, symbol: str):

        try:

            self.latency.start("pipeline")

            result = self.pipeline.step(symbol)

            self.latency.stop("pipeline")

            if result is None:

                return

            entry = result.get("entry")

            risk = result.get("risk")

            exit_signal = result.get("exit")

            if entry and risk:

                if self.risk_guardian.allow_trade():

                    size = risk.get("position_size", 0)

                    if size > 0:

                        order = self.execution.decide_order(

                            symbol,

                            {

                                "side": "BUY",

                                "size": size,

                                "price": entry.get("price"),

                                "bid": entry.get("bid"),

                                "ask": entry.get("ask"),

                                "liquidity": entry.get("liquidity")

                            }

                        )

                        if order:

                            logger.info(f"EXECUTE {order}")

                            self.risk_guardian.open_position()

            if exit_signal:

                if exit_signal.get("exit_signal"):

                    logger.info(f"EXIT {symbol}")

                    self.risk_guardian.close_position()

        except Exception:

            logger.exception("Symbol processing failed")

    # --------------------------------------------------------
    # Run engine
    # --------------------------------------------------------

    def run(self):

        logger.info("Ultimate AI Trading Engine started")

        self.running = True

        while self.running:

            start = time.time()

            for symbol in self.symbols:

                self.process_symbol(symbol)

            elapsed = time.time() - start

            sleep = max(0, 1 - elapsed)

            time.sleep(sleep)

    # --------------------------------------------------------
    # Stop engine
    # --------------------------------------------------------

    def stop(self):

        self.running = False


# ============================================================
# Singleton
# ============================================================

_engine = None


def get_ultimate_ai_trading_engine():

    global _engine

    if _engine is None:

        _engine = UltimateAITradingEngine()

    return _engine