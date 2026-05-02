# ============================================================
# latency_monitor_ai.py
#
# SYSTEM LATENCY MONITOR
#
# Monitors AI system performance
#
# ============================================================

from __future__ import annotations

import logging
import time
from typing import Dict

logger = logging.getLogger(__name__)


class LatencyMonitorAI:

    def __init__(self):

        self.metrics: Dict[str, float] = {}

        self.threshold = 0.5

    # --------------------------------------------------------
    # Start timer
    # --------------------------------------------------------

    def start(self, key: str):

        self.metrics[key] = time.time()

    # --------------------------------------------------------
    # End timer
    # --------------------------------------------------------

    def stop(self, key: str):

        start = self.metrics.get(key)

        if start is None:

            return

        latency = time.time() - start

        if latency > self.threshold:

            logger.warning(

                f"Latency warning {key}: {latency:.3f}s"

            )

        else:

            logger.debug(

                f"Latency {key}: {latency:.3f}s"

            )

        self.metrics.pop(key, None)

    # --------------------------------------------------------
    # Measure function
    # --------------------------------------------------------

    def measure(self, key, func, *args, **kwargs):

        self.start(key)

        result = func(*args, **kwargs)

        self.stop(key)

        return result


_monitor = None


def get_latency_monitor_ai():

    global _monitor

    if _monitor is None:

        _monitor = LatencyMonitorAI()

    return _monitor