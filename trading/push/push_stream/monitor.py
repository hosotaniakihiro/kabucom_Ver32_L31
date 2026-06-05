# ============================================================
# File   : trading/push/push_stream/monitor.py
# Version: Ver1.5-WS-DISCONNECTED-WATCHDOG-RECOVER
# ------------------------------------------------------------
# PUSH監視ログ。
#
# Ver1.5:
#   - connected=False / ws_alive=False のまま last_recv が止まるケースも
#     stale として検出する。
#   - 古い ws_app / sender を明示 close/clear して runner の再接続へ流す。
#
# Ver1.4:
#   - ws ping timeout / WinError 10054 後に connected_event が残る、または
#     sock.connected=True でも last_recv が止まるケースを検出
#   - stale が一定回数続いたら WebSocket を強制 close して runner の再接続へ流す
#   - 再接続後は on_open refresh が clear_first + unregister_first で再登録する
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import threading
import time

from .constants import MONITOR_INTERVAL_SEC
from . import state
from .runtime import _safe_iso, _sync_push_df_to_global, _safe_set_runtime
from .transport import _clear_sender, _is_ws_alive

logger = logging.getLogger(__name__)


VERSION = "Ver1.5-WS-DISCONNECTED-WATCHDOG-RECOVER"


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok"}
    except Exception:
        return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


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


def _seconds_since(value: object) -> float | None:
    try:
        if value is None:
            return None
        if isinstance(value, dt.datetime):
            return max(0.0, (dt.datetime.now() - value.replace(tzinfo=None)).total_seconds())
        return None
    except Exception:
        return None


def _force_close_ws_for_reconnect(reason: str, *, stale_age: float | None, stale_count: int) -> bool:
    """
    runner._run_forever_loop は run_forever から戻ると RECONNECT_WAIT_SEC 後に再接続する。
    ここでは古い sender / connected_event を落として ws_app.close() だけ実施する。
    """
    if not _env_bool("PUSH_STREAM_AUTO_RECOVER_WS_STALE", True):
        return False

    try:
        _safe_set_runtime("push_ws_stale_recovering", True)
        _safe_set_runtime("push_ws_stale_recover_reason", reason)
        _safe_set_runtime("push_ws_stale_age_sec", stale_age)
        _safe_set_runtime("push_ws_stale_count", stale_count)

        state._connected_event.clear()
        _safe_set_runtime("ws_connected", False)
        _clear_sender()

        ws_app = None
        try:
            with state._ws_state_lock:
                ws_app = state._ws_app
        except Exception:
            ws_app = None

        if ws_app is not None:
            try:
                ws_app.close()
            except Exception:
                logger.debug("[PUSH MONITOR][WS STALE] ws_app.close failed", exc_info=True)

        logger.warning(
            "[PUSH MONITOR][WS STALE RECOVER] force close requested reason=%s stale_age=%s stale_count=%d; runner will reconnect and rotation/on_open will refresh subscriptions",
            reason,
            None if stale_age is None else round(stale_age, 1),
            stale_count,
        )
        return True
    except Exception:
        logger.exception("[PUSH MONITOR][WS STALE RECOVER] failed reason=%s", reason)
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
    logger.info("[push_stream] monitor worker started version=%s", VERSION)
    loop_count = 0
    stall_count = 0
    ws_stale_count = 0
    last_seen_received = int(getattr(state, "_total_received", 0) or 0)

    while not state._stop_event.is_set():
        try:
            loop_count += 1
            df_rows = 0 if state._push_df is None else len(state._push_df)
            queue_size = state._push_queue.qsize()
            flush_alive = _thread_alive(getattr(state, "_flush_thread", None))
            writer_ready = getattr(state, "_stream_writer", None) is not None
            memory_only = _is_main_memory_only_mode()
            connected = state._connected_event.is_set()
            ws_alive = _is_ws_alive()
            total_received = int(getattr(state, "_total_received", 0) or 0)
            last_recv_age = _seconds_since(getattr(state, "_last_message_at", None))
            stale_limit_sec = max(15.0, _env_float("PUSH_STREAM_WS_STALE_SEC", 30.0))
            stale_trigger_count = max(2, int(_env_float("PUSH_STREAM_WS_STALE_COUNT", 2.0)))

            _sync_push_df_to_global()

            logger.info(
                "[PUSH MONITOR] %s connected=%s ws_alive=%s queue=%d df_rows=%d total_received=%d total_flushed=%d dropped=%d flush_alive=%s writer_ready=%s memory_only=%s last_recv=%s last_recv_age=%s last_flush=%s",
                loop_count,
                connected,
                ws_alive,
                queue_size,
                df_rows,
                total_received,
                state._total_flushed,
                state._total_dropped,
                flush_alive,
                writer_ready,
                memory_only,
                _safe_iso(state._last_message_at),
                None if last_recv_age is None else round(last_recv_age, 1),
                _safe_iso(state._last_flush_at),
            )

            # WebSocket watchdog:
            # - connected_event=True なのに sock が死んでいる
            # - sock.connected=True でも一定時間メッセージが来ない
            # - connected_event=False / ws_alive=False のまま最後の受信から一定時間が経過
            #   している。このケースでは runner がまだ run_forever 内で戻っていない、または
            #   古い ws_app が残っている可能性があるため、明示 close して再接続を促す。
            received_moved = total_received > last_seen_received
            last_seen_received = total_received
            ws_stale_reason = None
            if connected and not ws_alive:
                ws_stale_reason = "connected_event_true_but_ws_not_alive"
            elif connected and ws_alive and total_received > 0 and not received_moved and last_recv_age is not None and last_recv_age >= stale_limit_sec:
                ws_stale_reason = "last_recv_stale_while_ws_alive"
            elif (not connected) and (not ws_alive) and total_received > 0 and last_recv_age is not None and last_recv_age >= stale_limit_sec:
                ws_stale_reason = "disconnected_and_ws_not_alive_after_recv"
            elif (not connected) and (not ws_alive) and total_received <= 0 and loop_count >= 4:
                # 起動直後に接続が確立しないケースも早めに古いws_appを閉じる。
                ws_stale_reason = "startup_ws_never_connected"

            if ws_stale_reason:
                ws_stale_count += 1
                _safe_set_runtime("push_ws_stale", True)
                _safe_set_runtime("push_ws_stale_count", ws_stale_count)
                logger.warning(
                    "[PUSH MONITOR][WS STALE] reason=%s count=%d/%d last_recv_age=%s total_received=%d connected=%s ws_alive=%s",
                    ws_stale_reason,
                    ws_stale_count,
                    stale_trigger_count,
                    None if last_recv_age is None else round(last_recv_age, 1),
                    total_received,
                    connected,
                    ws_alive,
                )
                if ws_stale_count >= stale_trigger_count:
                    _force_close_ws_for_reconnect(ws_stale_reason, stale_age=last_recv_age, stale_count=ws_stale_count)
                    ws_stale_count = 0
            else:
                ws_stale_count = 0
                _safe_set_runtime("push_ws_stale", False)

            if memory_only:
                # main.py側ではflush workerが無いのが正常。
                # 古いqueueが残っていてもDB保存へ復旧させない。
                stall_count = 0
                _safe_set_runtime("push_stream_memory_only", True)
                _safe_set_runtime("push_writer_running", False)
                _safe_set_runtime("push_flush_stalled", False)
            else:
                if queue_size > 0 and not flush_alive:
                    stall_count += 1
                    _safe_set_runtime("push_flush_stalled", True)
                    _safe_set_runtime("push_flush_stall_count", stall_count)
                    logger.warning(
                        "[PUSH MONITOR][FLUSH STALL] queue=%d flush_alive=%s writer_ready=%s stall_count=%d",
                        queue_size,
                        flush_alive,
                        writer_ready,
                        stall_count,
                    )
                    if stall_count >= 2:
                        _recover_flush_worker_if_needed(
                            queue_size=queue_size,
                            flush_alive=flush_alive,
                            writer_ready=writer_ready,
                            stall_count=stall_count,
                        )
                        stall_count = 0
                else:
                    stall_count = 0
                    _safe_set_runtime("push_flush_stalled", False)

        except Exception:
            logger.exception("[push_stream] monitor worker loop failed")

        time.sleep(MONITOR_INTERVAL_SEC)

    logger.info("[push_stream] monitor worker stopped")
