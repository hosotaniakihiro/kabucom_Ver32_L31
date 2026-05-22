# ============================================================
# File   : trading/push/push_stream/monitor.py
# Version: Ver1.3-AUTO-RECOVER-MEMORY-ONLY-GUARD
# ------------------------------------------------------------
# PUSH監視ログ。
#
# Ver1.3:
#   - main.py memory-only mode では flush stall / auto recover を出さない
#   - main.py側で誤ってflush workerを自動再起動しない
#   - PUSH受信は memory df / latest cache / 5秒足 用として継続
#
# Ver1.2:
#   - flush_alive=False かつ queue>0 の場合、flush worker を自動再起動
#   - writer_ready=False の場合でも writers._ensure_stream_writer() に任せて遅延復旧
# ============================================================

from __future__ import annotations

import logging
import os
import threading
import time

from .constants import MONITOR_INTERVAL_SEC
from . import state
from .runtime import _safe_iso, _sync_push_df_to_global, _safe_set_runtime
from .transport import _is_ws_alive

logger = logging.getLogger(__name__)


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok"}
    except Exception:
        return bool(default)


def _thread_alive(th) -> bool:
    try:
        return bool(th and th.is_alive())
    except Exception:
        return False


def _is_main_memory_only_mode() -> bool:
    """
    main.py / main_database.py 分離運用時、main.py側はPUSH DB保存をしない。
    この場合、flush workerが無いことは正常なので、stall扱いやauto recoverをしない。
    """
    try:
        from data_collectors.split_mode import should_skip_data_collector_work_in_main
        return bool(should_skip_data_collector_work_in_main())
    except Exception:
        return False


def _recover_flush_worker_if_needed(*, queue_size: int, flush_alive: bool, writer_ready: bool, stall_count: int) -> bool:
    """PUSH受信はあるがflush workerが死んでいる場合に再起動する。"""
    if _is_main_memory_only_mode():
        _safe_set_runtime("push_stream_memory_only", True)
        _safe_set_runtime("push_writer_running", False)
        logger.warning(
            "[PUSH MONITOR][MEMORY ONLY] auto recover skipped in main process queue=%d stall_count=%d; main_database.py handles PUSH DB storage",
            queue_size,
            stall_count,
        )
        return False

    if not _env_bool("PUSH_STREAM_AUTO_RECOVER_FLUSH", True):
        return False
    if queue_size <= 0:
        return False
    if flush_alive:
        return False
    try:
        from .writers import _flush_worker, _ensure_stream_writer

        writer = getattr(state, "_stream_writer", None)
        if writer is None:
            try:
                writer = _ensure_stream_writer()
            except Exception:
                logger.exception("[PUSH MONITOR][AUTO RECOVER] ensure stream writer failed")
                writer = None

        th = threading.Thread(target=_flush_worker, name="push-flush-worker-auto-recover", daemon=True)
        state._flush_thread = th
        th.start()
        _safe_set_runtime("push_writer_running", True)
        _safe_set_runtime("push_flush_auto_recovered", True)
        logger.warning(
            "[PUSH MONITOR][AUTO RECOVER] flush worker restarted queue=%d writer_ready_before=%s writer_now=%s stall_count=%d",
            queue_size,
            writer_ready,
            writer is not None,
            stall_count,
        )
        return True
    except Exception:
        logger.exception("[PUSH MONITOR][AUTO RECOVER] failed queue=%d", queue_size)
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
            memory_only = _is_main_memory_only_mode()

            _sync_push_df_to_global()

            logger.info(
                "[PUSH MONITOR] %s connected=%s ws_alive=%s queue=%d df_rows=%d total_received=%d total_flushed=%d dropped=%d flush_alive=%s writer_ready=%s memory_only=%s last_recv=%s last_flush=%s",
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
                memory_only,
                _safe_iso(state._last_message_at),
                _safe_iso(state._last_flush_at),
            )

            if memory_only:
                # main.py側ではflush workerが無いのが正常。
                # 古いqueueが残っていてもDB保存へ復旧させない。
                stall_count = 0
                _safe_set_runtime("push_stream_memory_only", True)
                _safe_set_runtime("push_writer_running", False)
                _safe_set_runtime("push_flush_stalled", False)
                if queue_size > 0:
                    try:
                        with state._push_queue.mutex:
                            state._push_queue.queue.clear()
                    except Exception:
                        logger.debug("[PUSH MONITOR][MEMORY ONLY] queue clear skipped", exc_info=True)
                    logger.warning(
                        "[PUSH MONITOR][MEMORY ONLY] cleared DB queue in main process queue_before=%d",
                        queue_size,
                    )
                time.sleep(MONITOR_INTERVAL_SEC)
                continue

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
                _recover_flush_worker_if_needed(
                    queue_size=queue_size,
                    flush_alive=flush_alive,
                    writer_ready=writer_ready,
                    stall_count=stall_count,
                )
            else:
                stall_count = 0
                _safe_set_runtime("push_flush_stalled", False)

        except Exception:
            logger.exception("[push_stream] monitor worker failed")

        time.sleep(MONITOR_INTERVAL_SEC)

    logger.info("[push_stream] monitor worker stopped")


__all__ = ["_monitor_worker"]