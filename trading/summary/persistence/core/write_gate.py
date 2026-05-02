# ============================================================
# File   : trading/summary/persistence/core/write_gate.py
# Version: PRODUCTION-STABLE-REV1.0-SUMMARY-WRITE-GATE
# ------------------------------------------------------------
# Purpose:
#   summary DB 書き込みをプロセス内で一本化する。
# ============================================================

from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from typing import Iterator, Optional

logger = logging.getLogger(__name__)

_SUMMARY_WRITE_LOCK = threading.RLock()


@contextmanager
def summary_write_gate(
    *,
    table_name: str = "",
    interval: int | str = "",
    reason: str = "upsert",
    timeout: Optional[float] = None,
    skip_if_busy: bool = False,
) -> Iterator[bool]:
    started = time.time()
    acquired = False

    try:
        if skip_if_busy:
            acquired = _SUMMARY_WRITE_LOCK.acquire(blocking=False)
            if not acquired:
                logger.warning(
                    "[SUMMARY WRITE GATE] busy -> skip table=%s interval=%s reason=%s",
                    table_name,
                    interval,
                    reason,
                )
                yield False
                return

        elif timeout is not None:
            acquired = _SUMMARY_WRITE_LOCK.acquire(timeout=max(0.0, float(timeout)))
            if not acquired:
                logger.warning(
                    "[SUMMARY WRITE GATE] timeout table=%s interval=%s reason=%s timeout=%.2fs",
                    table_name,
                    interval,
                    reason,
                    float(timeout),
                )
                yield False
                return

        else:
            _SUMMARY_WRITE_LOCK.acquire()
            acquired = True

        wait_sec = time.time() - started
        if wait_sec >= 0.5:
            logger.info(
                "[SUMMARY WRITE GATE] acquired after wait table=%s interval=%s reason=%s wait=%.3fs",
                table_name,
                interval,
                reason,
                wait_sec,
            )

        yield True

    finally:
        if acquired:
            try:
                _SUMMARY_WRITE_LOCK.release()
            except Exception:
                logger.exception(
                    "[SUMMARY WRITE GATE] release failed table=%s interval=%s reason=%s",
                    table_name,
                    interval,
                    reason,
                )


def is_summary_write_locked() -> bool:
    acquired = _SUMMARY_WRITE_LOCK.acquire(blocking=False)
    if acquired:
        _SUMMARY_WRITE_LOCK.release()
        return False
    return True
