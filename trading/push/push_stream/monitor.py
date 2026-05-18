# ============================================================
# File   : trading/push/push_stream/monitor.py
# Version: Ver1.1-PUSH-STREAM-MONITOR-FLUSH-STALL-DETECT
# ------------------------------------------------------------
# PUSH監視ログ。
#
# Ver1.1:
#   - queue が溜まっているのに total_flushed=0 / last_flush=None の状態をERROR化
#   - flush thread alive / stream_writer 有無をログへ追加
#   - サマリー遅延の原因になりやすい writer停止を早期発見する
# ============================================================

from __future__ import annotations

import logging
import time

from .constants import MONITOR_INTERVAL_SEC
from . import state
from .runtime import _safe_iso, _sync_push_df_to_global, _safe_set_runtime
from .transport import _is_ws_alive

logger = logging.getLogger(__name__)


def _thread_alive(th) -> bool:
    try:
        return bool(th and th.is_alive())
    except Exception:
        return False


def _monitor_worker() -> None:
    logger.info("[push_stream] monitor worker started")
    loop_count = 0
    stall_count = 0

    while not state._stop_event.is_set():
        try:
            loop_count += 1
            df_rows = 0 if state._push_df is None else len(state._push_df)
            queue_size = state._push_queue.qsize()
            flush_alive = _thread_alive(getattr(state, "_flush_thread", None))
            writer_ready = getattr(state, "_stream_writer", None) is not None

            _sync_push_df_to_global()

            logger.info(
                "[PUSH MONITOR] %s connected=%s ws_alive=%s queue=%d df_rows=%d total_received=%d total_flushed=%d dropped=%d flush_alive=%s writer_ready=%s last_recv=%s last_flush=%s",
                loop_count,
                state._connected_event.is_set(),
                _is_ws_alive(),
                queue_size,
                df_rows,
                state._total_received,
                state._total_flushed,
                state._total_dropped,
                flush_alive,
                writer_ready,
                _safe_iso(state._last_message_at),
                _safe_iso(state._last_flush_at),
            )

            # 受信しているのにDB flushが一度も成功していない状態を強く検知。
            if queue_size > 0 and state._total_received > 0 and state._total_flushed <= 0:
                stall_count += 1
                _safe_set_runtime("push_flush_stalled", True)
                _safe_set_runtime("push_flush_stall_count", stall_count)
                logger.error(
                    "[PUSH MONITOR][FLUSH STALL] queue=%d received=%d flushed=%d flush_alive=%s writer_ready=%s stall_count=%d last_recv=%s last_flush=%s",
                    queue_size,
                    state._total_received,
                    state._total_flushed,
                    flush_alive,
                    writer_ready,
                    stall_count,
                    _safe_iso(state._last_message_at),
                    _safe_iso(state._last_flush_at),
                )
            else:
                stall_count = 0
                _safe_set_runtime("push_flush_stalled", False)

        except Exception:
            logger.exception("[push_stream] monitor worker failed")

        time.sleep(MONITOR_INTERVAL_SEC)

    logger.info("[push_stream] monitor worker stopped")
