from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)
_INSTALLED = False
_ORIGINAL = None


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return bool(default)
    s = str(v).strip().lower()
    if s in {"1", "true", "yes", "y", "on", "ok"}:
        return True
    if s in {"0", "false", "no", "n", "off", "ng"}:
        return False
    return bool(default)


def _alive(th: Any) -> bool:
    try:
        return bool(th and th.is_alive())
    except Exception:
        return False


def _restart_flush_worker(reason: str) -> bool:
    if not _env_bool("PUSH_STREAM_AUTO_RECOVER_FLUSH", True):
        return False
    try:
        from trading.push.push_stream import state
        from trading.push.push_stream.writers import _flush_worker, _init_stream_writer
        from trading.push.push_stream.runtime import _safe_set_runtime

        if _alive(getattr(state, "_flush_thread", None)):
            return True

        if getattr(state, "_stream_writer", None) is None:
            state._stream_writer = _init_stream_writer()

        th = threading.Thread(target=_flush_worker, name="push-flush-worker-auto-recover", daemon=True)
        state._flush_thread = th
        th.start()
        _safe_set_runtime("push_writer_running", True)
        _safe_set_runtime("push_flush_auto_recovered", True)
        logger.warning(
            "[PUSH FLUSH AUTO RECOVER] restarted reason=%s queue=%s writer_ready=%s",
            reason,
            state._push_queue.qsize(),
            getattr(state, "_stream_writer", None) is not None,
        )
        return True
    except Exception:
        logger.exception("[PUSH FLUSH AUTO RECOVER] restart failed reason=%s", reason)
        return False


def _patched_monitor_worker() -> None:
    from trading.push.push_stream import state
    from trading.push.push_stream.constants import MONITOR_INTERVAL_SEC
    from trading.push.push_stream.runtime import _safe_iso, _sync_push_df_to_global, _safe_set_runtime
    from trading.push.push_stream.transport import _is_ws_alive

    logger.info("[push_stream] patched monitor worker started")
    loop_count = 0
    stall_count = 0
    while not state._stop_event.is_set():
        try:
            loop_count += 1
            df_rows = 0 if state._push_df is None else len(state._push_df)
            queue_size = state._push_queue.qsize()
            flush_alive = _alive(getattr(state, "_flush_thread", None))
            writer_ready = getattr(state, "_stream_writer", None) is not None
            _sync_push_df_to_global()
            logger.info(
                "[PUSH MONITOR] %s connected=%s ws_alive=%s queue=%d df_rows=%d total_received=%d total_flushed=%d dropped=%d flush_alive=%s writer_ready=%s last_recv=%s last_flush=%s",
                loop_count, state._connected_event.is_set(), _is_ws_alive(), queue_size, df_rows,
                state._total_received, state._total_flushed, state._total_dropped, flush_alive, writer_ready,
                _safe_iso(state._last_message_at), _safe_iso(state._last_flush_at),
            )
            if queue_size > 0 and state._total_received > 0 and (not flush_alive or not writer_ready):
                _restart_flush_worker(f"monitor_not_ready_flush={flush_alive}_writer={writer_ready}")
                flush_alive = _alive(getattr(state, "_flush_thread", None))
                writer_ready = getattr(state, "_stream_writer", None) is not None
            if queue_size > 0 and state._total_received > 0 and state._total_flushed <= 0:
                stall_count += 1
                _safe_set_runtime("push_flush_stalled", True)
                _safe_set_runtime("push_flush_stall_count", stall_count)
                logger.error(
                    "[PUSH MONITOR][FLUSH STALL] queue=%d received=%d flushed=%d flush_alive=%s writer_ready=%s stall_count=%d last_recv=%s last_flush=%s",
                    queue_size, state._total_received, state._total_flushed, flush_alive, writer_ready, stall_count,
                    _safe_iso(state._last_message_at), _safe_iso(state._last_flush_at),
                )
            else:
                stall_count = 0
                _safe_set_runtime("push_flush_stalled", False)
        except Exception:
            logger.exception("[push_stream] patched monitor worker failed")
        time.sleep(MONITOR_INTERVAL_SEC)
    logger.info("[push_stream] patched monitor worker stopped")


def install() -> bool:
    global _INSTALLED, _ORIGINAL
    if _INSTALLED:
        return True
    try:
        import trading.push.push_stream.monitor as mon
        old = getattr(mon, "_monitor_worker", None)
        if callable(old) and getattr(old, "_push_flush_auto_recover_v1", False):
            _INSTALLED = True
            return True
        _ORIGINAL = old
        _patched_monitor_worker._push_flush_auto_recover_v1 = True  # type: ignore[attr-defined]
        mon._monitor_worker = _patched_monitor_worker
        _INSTALLED = True
        logger.warning("[PUSH FLUSH AUTO RECOVER] installed")
        return True
    except Exception:
        logger.exception("[PUSH FLUSH AUTO RECOVER] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[PUSH FLUSH AUTO RECOVER] auto install failed")

__all__ = ["install"]
