# ============================================================
# File   : trading/push/push_stream/monitor.py
# Version: Ver1.0-PUSH-STREAM-MONITOR
# ============================================================

from __future__ import annotations

import logging
import time

from .constants import MONITOR_INTERVAL_SEC
from . import state
from .runtime import _safe_iso, _sync_push_df_to_global
from .transport import _is_ws_alive

logger = logging.getLogger(__name__)


def _monitor_worker() -> None:
    logger.info("[push_stream] monitor worker started")
    loop_count = 0

    while not state._stop_event.is_set():
        try:
            loop_count += 1
            df_rows = 0 if state._push_df is None else len(state._push_df)

            _sync_push_df_to_global()

            logger.info(
                "[PUSH MONITOR] %s connected=%s ws_alive=%s queue=%d df_rows=%d total_received=%d total_flushed=%d dropped=%d last_recv=%s last_flush=%s",
                loop_count,
                state._connected_event.is_set(),
                _is_ws_alive(),
                state._push_queue.qsize(),
                df_rows,
                state._total_received,
                state._total_flushed,
                state._total_dropped,
                _safe_iso(state._last_message_at),
                _safe_iso(state._last_flush_at),
            )
        except Exception:
            logger.exception("[push_stream] monitor worker failed")

        time.sleep(MONITOR_INTERVAL_SEC)

    logger.info("[push_stream] monitor worker stopped")