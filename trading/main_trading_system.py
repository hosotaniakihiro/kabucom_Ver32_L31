# ============================================================
# File   : trading/main_trading_system.py
# Version: Ver1.0-PRODUCTION-MAIN
# ------------------------------------------------------------
# Main Trading System Entry Point
#
# Responsibilities
#   • Initialize system components
#   • Start trading engine
#   • Process market data feed
#   • Health monitoring
#   • Graceful shutdown
# ============================================================

from __future__ import annotations

import logging
import signal
import time
import threading
from typing import Dict

from trading.core.trading_engine import trading_engine

logger = logging.getLogger(__name__)

running = True


# ============================================================
# Market Data Simulator (Replace with real feed)
# ============================================================

def market_data_feed():

    while running:

        try:

            # ------------------------------------------------
            # Simulated market data
            # ------------------------------------------------

            symbol = "7203"

            orderbook = {
                "best_bid": 2500.0,
                "best_ask": 2500.5,
                "bids": [(2499.5, 1000), (2499.0, 1200)],
                "asks": [(2500.5, 900), (2501.0, 1100)],
            }

            trades = [
                {"price": 2500.2, "size": 100, "side": "BUY"},
                {"price": 2500.1, "size": 50, "side": "SELL"},
            ]

            prices = [2499.8, 2500.0, 2500.2]

            portfolio_state = {
                "risk": 0.01,
                "volatility": 0.01,
                "drawdown": -0.005,
                "daily_pnl": 0.002,
                "correlation": 0.2
            }

            # ------------------------------------------------
            # Trading Engine
            # ------------------------------------------------

            trading_engine.process_market_tick(
                symbol=symbol,
                orderbook=orderbook,
                trades=trades,
                prices=prices,
                portfolio_state=portfolio_state
            )

            trading_engine.update_market_price(
                symbol,
                prices[-1]
            )

            time.sleep(0.5)

        except Exception:

            logger.exception("[MAIN] market data loop error")


# ============================================================
# Health Monitor
# ============================================================

def health_monitor():

    while running:

        try:

            status = trading_engine.status()

            logger.info(
                "[HEALTH] engine=%s orders=%s positions=%s",
                status["engine_running"],
                status["oms"]["total_orders"],
                status["oms"]["positions"]
            )

            time.sleep(5)

        except Exception:

            logger.exception("[MAIN] health monitor error")


# ============================================================
# Shutdown Handler
# ============================================================

def shutdown(signum, frame):

    global running

    logger.warning("[MAIN] shutdown signal received")

    running = False


# ============================================================
# Main
# ============================================================

def main():

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )

    logger.info("Starting AI Trading System")

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    market_thread = threading.Thread(
        target=market_data_feed,
        daemon=True
    )

    health_thread = threading.Thread(
        target=health_monitor,
        daemon=True
    )

    market_thread.start()
    health_thread.start()

    try:

        while running:

            time.sleep(1)

    except KeyboardInterrupt:

        shutdown(None, None)

    logger.info("Trading system stopped")


# ============================================================
# Entry
# ============================================================

if __name__ == "__main__":

    main()