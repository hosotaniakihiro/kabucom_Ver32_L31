# ============================================================
# scheduler_ai.py
#
# AI SYSTEM SCHEDULER
#
# Runs AI pipeline in real time
#
# ============================================================

from __future__ import annotations

import logging
import time
from typing import List

from trading.data.data_pipeline_ai import get_data_pipeline_ai

logger = logging.getLogger(__name__)


class SchedulerAI:

    def __init__(self):

        self.pipeline = get_data_pipeline_ai()

        self.symbols: List[str] = []

        self.interval = 1.0

        self.running = False

    # --------------------------------------------------------
    # Register symbols
    # --------------------------------------------------------

    def set_symbols(self, symbols: List[str]):

        self.symbols = symbols

    # --------------------------------------------------------
    # Main loop
    # --------------------------------------------------------

    def run(self):

        logger.info("AI Scheduler started")

        self.running = True

        while self.running:

            start = time.time()

            for symbol in self.symbols:

                try:

                    result = self.pipeline.step(symbol)

                    if result:

                        logger.debug(f"{symbol} {result}")

                except Exception:

                    logger.exception("Scheduler step failed")

            elapsed = time.time() - start

            sleep = max(0, self.interval - elapsed)

            time.sleep(sleep)

    # --------------------------------------------------------
    # Stop scheduler
    # --------------------------------------------------------

    def stop(self):

        self.running = False


_scheduler = None


def get_scheduler_ai():

    global _scheduler

    if _scheduler is None:

        _scheduler = SchedulerAI()

    return _scheduler