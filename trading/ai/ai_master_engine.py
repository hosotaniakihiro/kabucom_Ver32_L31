# ============================================================
# trading/ai/ai_master_engine.py
# PRODUCTION AI MASTER ENGINE
# Ver2.0 ULTRA HFT STABLE
#
# Central orchestrator for full AI trading stack
# ============================================================

from __future__ import annotations

import logging
import threading
import time
from typing import Dict, List, Optional

from trading.ai.feature_store_ai import get_feature_store_ai
from trading.ai.smart_money_ai import get_smart_money_ai
from trading.ai.liquidity_grab_ai import get_liquidity_grab_ai
from trading.ai.alpha_fusion_ai import get_alpha_fusion_ai
from trading.ai.meta_entry_ai import get_meta_entry_ai
from trading.ai.meta_exit_ai import get_meta_exit_ai
from trading.ai.risk_manager_ai import get_risk_manager_ai
from trading.ai.execution_ai import get_execution_ai
from trading.ai.portfolio_ai import get_portfolio_ai
from trading.ai.market_regime_ai import get_market_regime_ai

logger = logging.getLogger(__name__)


# ============================================================
# AI MASTER ENGINE
# ============================================================

class AIMasterEngine:

    def __init__(self):

        # -------------------------------
        # AI modules
        # -------------------------------

        self.feature_store = get_feature_store_ai()

        self.smart_money_ai = get_smart_money_ai()
        self.liquidity_grab_ai = get_liquidity_grab_ai()

        self.alpha_ai = get_alpha_fusion_ai()

        self.entry_ai = get_meta_entry_ai()
        self.exit_ai = get_meta_exit_ai()

        self.risk_ai = get_risk_manager_ai()
        self.execution_ai = get_execution_ai()

        self.portfolio_ai = get_portfolio_ai()
        self.regime_ai = get_market_regime_ai()

        # -------------------------------
        # state
        # -------------------------------

        self.symbols: List[str] = []

        self.running = False

        self.loop_interval = 1.0

        self._last_cycle = 0.0

        # duplicate execution protection
        self._last_trade_ts: Dict[str, float] = {}

        # cache
        self._regime_cache: Optional[str] = None

        logger.info("[AI MASTER] initialized")

    # --------------------------------------------------------
    # symbol registration
    # --------------------------------------------------------

    def set_symbols(self, symbols: List[str]):

        if not symbols:
            return

        self.symbols = list(symbols)

        logger.info(
            "[AI MASTER] symbols registered %s",
            len(symbols)
        )

    # --------------------------------------------------------
    # start
    # --------------------------------------------------------

    def start(self):

        if self.running:
            return

        self.running = True

        th = threading.Thread(
            target=self._run_loop,
            daemon=True
        )

        th.start()

        logger.info("[AI MASTER] engine started")

    # --------------------------------------------------------
    # stop
    # --------------------------------------------------------

    def stop(self):

        self.running = False

        logger.info("[AI MASTER] engine stopped")

    # --------------------------------------------------------
    # main loop
    # --------------------------------------------------------

    def _run_loop(self):

        logger.info("[AI MASTER] loop started")

        while self.running:

            try:

                self._cycle()

            except Exception:

                logger.exception("[AI MASTER] cycle error")

            time.sleep(self.loop_interval)

    # --------------------------------------------------------
    # main evaluation cycle
    # --------------------------------------------------------

    def _cycle(self):

        now = time.time()

        if now - self._last_cycle < self.loop_interval:
            return

        self._last_cycle = now

        # ----------------------------------------
        # market regime update
        # ----------------------------------------

        try:

            self._regime_cache = self.regime_ai.get_regime()

        except Exception:

            logger.exception("[AI MASTER] regime error")

        # ----------------------------------------
        # iterate symbols
        # ----------------------------------------

        for symbol in self.symbols:

            try:

                self._process_symbol(symbol)

            except Exception:

                logger.exception(
                    "[AI MASTER] symbol failure %s",
                    symbol
                )

        # --------------------------------------------------------
        # symbol processing
        # --------------------------------------------------------

        def _process_symbol(self, symbol: str):

            # ----------------------------------------
            # feature generation
            # ----------------------------------------

            features = self.feature_store.build_features(symbol)

            if not features:
                return

            trades = features.get("trades")
            orderbook = features.get("orderbook")
            market_row = features.get("market_row")
            df = features.get("df")

            bid_volume = features.get("bid_volume", 0)
            ask_volume = features.get("ask_volume", 0)
            spread = features.get("spread", 0)
            vwap = features.get("vwap", 0)

            volume = features.get("volume", 0)
            volume_avg = features.get("volume_avg", 0)

            high = features.get("high", 0)
            low = features.get("low", 0)
            close = features.get("close", 0)

            # ----------------------------------------
            # smart money detection
            # ----------------------------------------

            smart_money = self.smart_money_ai.detect(
                symbol,
                trades,
                orderbook
            )

            # ----------------------------------------
            # liquidity grab detection
            # ----------------------------------------

            liquidity_event = self.liquidity_grab_ai.detect(
                symbol,
                trades,
                orderbook,
                df
            )

            # ----------------------------------------
            # alpha generation
            # ----------------------------------------

            alpha = self.alpha_ai.generate_alpha(

                trades,
                orderbook,
                market_row,
                df,

                bid_volume,
                ask_volume,
                spread,
                vwap,

                volume,
                volume_avg,

                high,
                low,
                close,

                smart_money,
                liquidity_event,
                self._regime_cache
            )

            if not alpha:
                return

            # ----------------------------------------
            # portfolio state
            # ----------------------------------------

            position = self.portfolio_ai.get_position(symbol)

            # ----------------------------------------
            # entry evaluation
            # ----------------------------------------

            entry = self.entry_ai.evaluate_entry(
                symbol,
                alpha,
                features,
                position,
                self._regime_cache
            )

            # ----------------------------------------
            # exit evaluation
            # ----------------------------------------

            exit_signal = self.exit_ai.evaluate_exit(
                symbol,
                alpha,
                features,
                position
            )

            # ----------------------------------------
            # risk validation
            # ----------------------------------------

            risk_ok = self.risk_ai.check_trade(
                symbol,
                alpha,
                position,
                self._regime_cache
            )

            if not risk_ok:
                return

            # ----------------------------------------
            # duplicate trade protection
            # ----------------------------------------

            now = time.time()

            last = self._last_trade_ts.get(symbol, 0)

            if now - last < 1.0:
                return

            # ----------------------------------------
            # ENTRY EXECUTION
            # ----------------------------------------

            if entry and entry.get("action") == "BUY":

                self.execution_ai.buy(
                    symbol,
                    entry
                )

                self._last_trade_ts[symbol] = now

                logger.info(
                    "[AI MASTER] BUY %s",
                    symbol
                )

            elif entry and entry.get("action") == "SELL":

                self.execution_ai.sell(
                    symbol,
                    entry
                )

                self._last_trade_ts[symbol] = now

                logger.info(
                    "[AI MASTER] SELL %s",
                    symbol
                )

            # ----------------------------------------
            # EXIT EXECUTION
            # ----------------------------------------

            if exit_signal and exit_signal.get("action") == "EXIT":
                self.execution_ai.exit(
                    symbol,
                    exit_signal
                )

                self._last_trade_ts[symbol] = now

                logger.info(
                    "[AI MASTER] EXIT %s",
                    symbol
                )

    # ============================================================
    # SINGLETON
    # ============================================================

    _engine: Optional[AIMasterEngine] = None

    def get_ai_master_engine() -> AIMasterEngine:

        global _engine

        if _engine is None:
            _engine = AIMasterEngine()

        return _engine