# ============================================================
# File   : trading/summary/persistence/helpers/locks.py
# Version: Ver1.0-SUMMARY-LOCKS
# ------------------------------------------------------------
# interval lock helper
# ============================================================

from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager

logger = logging.getLogger(__name__)

_INTERVAL_LOCKS: dict[int, threading.RLock] = {
    1: threading.RLock(),
    3: threading.RLock(),
    5: threading.RLock(),
    10: threading.RLock(),
    15: threading.RLock(),
    30: threading.RLock(),
    60: threading.RLock(),
    1440: threading.RLock(),
}

DEFAULT_LOCK_TIMEOUT_SEC = 60.0


class SummaryBusySkip(Exception):
    pass


@contextmanager
def _interval_lock(interval: int, timeout_sec: float = DEFAULT_LOCK_TIMEOUT_SEC, skip_if_busy: bool = False):
    lock = _INTERVAL_LOCKS.setdefault(int(interval), threading.RLock())
    started = time.monotonic()
    acquired = lock.acquire(timeout=float(timeout_sec))
    waited = time.monotonic() - started

    if not acquired:
        logger.warning(
            "[SUMMARY] interval lock acquire timeout: interval=%s waited=%.3fs timeout=%.3fs skip_if_busy=%s",
            interval,
            waited,
            float(timeout_sec),
            bool(skip_if_busy),
        )
        if skip_if_busy:
            raise SummaryBusySkip(
                f"interval lock busy interval={interval} waited={waited:.3f}s timeout={float(timeout_sec):.3f}s"
            )
        raise TimeoutError(
            f"interval lock acquire timeout interval={interval} waited={waited:.3f}s timeout={float(timeout_sec):.3f}s"
        )

    try:
        yield waited
    finally:
        try:
            lock.release()
        except Exception:
            logger.debug("[SUMMARY] interval lock release failed interval=%s", exc_info=True)