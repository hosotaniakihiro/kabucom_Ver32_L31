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


def ws_snapshot() -> dict:
    """WebSocket の同一性・接続状態を「1 回のロック内で」まとめて取る。

    2026-09-01 12:57:35 のログでは
        ws_state=CONNECTED_RECEIVING connected=True ws_alive=True
        ws_instance_id=none  callback_bound=no_ws
    という矛盾した行が出ていた。connected / ws_alive / ws_instance_id を
    別々のタイミングで読んでいたため、runner が ws_app を差し替える瞬間に
    「接続中なのにインスタンスが無い」状態が観測されていた。

    ここで一貫した 1 枚のスナップショットにし、
    インスタンスが無いなら接続中とは呼ばない (connected を False に倒す) 。
    """
    info = {
        "instance_id": "none",
        "generation": None,
        "connected": False,
        "ws_alive": False,
        "callback_bound": "no_ws",
        "inconsistent": False,
    }
    try:
        with state._ws_state_lock:
            ws_app = getattr(state, "_ws_app", None)
            event_connected = bool(state._connected_event.is_set())
            sock = getattr(ws_app, "sock", None) if ws_app is not None else None
            bound = getattr(ws_app, "on_message", None) if ws_app is not None else None
            alive = bool(sock is not None and getattr(sock, "connected", False))

        if ws_app is None:
            info["connected"] = False
            info["ws_alive"] = False
            # 「イベントは立っているのに ws_app が無い」= 差し替え中の過渡状態。
            # 接続中として扱うと watchdog が誤作動するので必ず落とす。
            info["inconsistent"] = bool(event_connected)
        else:
            info["instance_id"] = "%x/sock=%s" % (
                id(ws_app), "none" if sock is None else ("%x" % id(sock))
            )
            info["connected"] = bool(event_connected)
            info["ws_alive"] = alive
            try:
                from .ws_callbacks import on_message as _on_message

                info["callback_bound"] = (
                    "yes" if bound is _on_message
                    else ("other:%s" % getattr(bound, "__name__", bound))
                )
            except Exception:
                info["callback_bound"] = "unknown"

        try:
            from .ws_callbacks import current_connection_generation

            info["generation"] = current_connection_generation()
        except Exception:
            info["generation"] = None
    except Exception:
        logger.debug("[PUSH MONITOR] ws snapshot failed", exc_info=True)
    return info


def _ws_instance_id() -> str:
    """今 monitor が見ている WebSocketApp の同一性。

    rotation/再接続で ws_app が差し替わったのに古いインスタンスを
    見ていないかを、ログだけで確認できるようにする。
    """
    try:
        with state._ws_state_lock:
            ws_app = getattr(state, "_ws_app", None)
        if ws_app is None:
            return "none"
        sock = getattr(ws_app, "sock", None)
        return "%x/sock=%s" % (id(ws_app), "none" if sock is None else ("%x" % id(sock)))
    except Exception:
        return "unknown"


def _connection_generation():
    try:
        from .ws_callbacks import current_connection_generation

        return current_connection_generation()
    except Exception:
        return None


def _callback_bound() -> str:
    """現行 ws_app に on_message が実際に結び付いているか。"""
    try:
        with state._ws_state_lock:
            ws_app = getattr(state, "_ws_app", None)
        if ws_app is None:
            return "no_ws"
        from .ws_callbacks import on_message as _on_message

        bound = getattr(ws_app, "on_message", None)
        return "yes" if bound is _on_message else ("other:%s" % getattr(bound, "__name__", bound))
    except Exception:
        return "unknown"


def _registered_head() -> tuple:
    """kabu Station へ登録済みの銘柄数と先頭。REST は叩かず runtime 値だけ見る。"""
    try:
        from global_state import global_data

        syms = getattr(global_data, "push_registered_symbols", None)
        if not syms:
            syms = getattr(global_data, "register_symbols", None)
        if syms is None:
            return (None, None)
        syms = list(syms)
        return (len(syms), syms[:5])
    except Exception:
        return (None, None)


def _seconds_since(value: object) -> float | None:
    try:
        if value is None:
            return None
        if isinstance(value, dt.datetime):
            return max(0.0, (dt.datetime.now() - value.replace(tzinfo=None)).total_seconds())
        return None
    except Exception:
        return None


def _registration_is_broken() -> tuple[bool, str]:
    """直近の REST 銘柄登録が失敗したままか。

    (壊れているか, ログ用の詳細) を返す。
    成功実績が無く失敗だけがある場合も「壊れている」と見なす。
    """
    if not _env_bool("PUSH_STREAM_WS_STALE_SKIP_WHEN_REGISTER_BROKEN", True):
        return False, ""
    try:
        ok_at = getattr(state, "_last_register_ok_at", None)
        fail_at = getattr(state, "_last_register_fail_at", None)
        if fail_at is None:
            return False, ""
        if ok_at is not None and ok_at >= fail_at:
            return False, ""
        import datetime as _dt

        age = (_dt.datetime.now() - fail_at).total_seconds()
        return True, "last_register_fail_at=%s age=%.1fs last_register_ok_at=%s" % (fail_at, age, ok_at)
    except Exception:
        logger.debug("[PUSH MONITOR] registration broken check failed", exc_info=True)
        return False, ""


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

        # 2026-10-29: 自分で閉じたことを runner に伝える。場中の再接続の待ちの「続いた回数」に数えない
        #   (昼休み・寄り前の自己切断で回数が積み上がり、場が開いた時点で 15 秒待ちになっていた)。
        try:
            from .ws_callbacks import note_disconnect_info

            note_disconnect_info(self_close_reason=reason)
        except Exception:
            logger.debug("[PUSH MONITOR][WS STALE] note self close failed", exc_info=True)

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
            # Ver2.0: 行数は deque から数える (materialize しない)。
            df_rows = len(getattr(state, "_push_rows", ()) or ())
            queue_size = state._push_queue.qsize()
            flush_alive = _thread_alive(getattr(state, "_flush_thread", None))
            writer_ready = getattr(state, "_stream_writer", None) is not None
            memory_only = _is_main_memory_only_mode()
            snap = ws_snapshot()
            connected = bool(snap["connected"])
            ws_alive = bool(snap["ws_alive"])
            if snap["inconsistent"]:
                # ws_app が差し替え中。接続中とは扱わない。
                logger.warning(
                    "[PUSH MONITOR][WS IDENTITY] connected_event=True なのに ws_app が無い "
                    "-> DISCONNECTED として扱う generation=%s (再接続の過渡状態)",
                    snap["generation"],
                )
            total_received = int(getattr(state, "_total_received", 0) or 0)
            last_recv_age = _seconds_since(getattr(state, "_last_message_at", None))
            stale_limit_sec = max(15.0, _env_float("PUSH_STREAM_WS_STALE_SEC", 30.0))
            stale_trigger_count = max(2, int(_env_float("PUSH_STREAM_WS_STALE_COUNT", 2.0)))

            _sync_push_df_to_global(reason="monitor")

            # ------------------------------------------------------------
            # 状態表示
            #   connected=True / total_received=0 / last_recv=None を
            #   「接続済み」と出していたため、PUSH を所有していない
            #   main.py 側の行が「繋がっているのに受信 0」に見えていた。
            #   所有していないなら NOT_OWNER、所有していて無音なら
            #   CONNECTED_NO_DATA として区別する。
            # ------------------------------------------------------------
            no_data_sec = max(5.0, _env_float("PUSH_MONITOR_NO_DATA_SEC", 20.0))
            if not connected:
                ws_state = "DISCONNECTED"
            elif total_received <= 0 or last_recv_age is None:
                ws_state = "CONNECTED_NO_DATA"
            elif last_recv_age >= no_data_sec:
                ws_state = "CONNECTED_NO_DATA"
            else:
                ws_state = "CONNECTED_RECEIVING"

            logger.info(
                "[PUSH MONITOR] %s ws_state=%s connected=%s ws_alive=%s queue=%d df_rows=%d "
                "total_received=%d total_flushed=%d dropped=%d flush_alive=%s writer_ready=%s "
                "memory_only=%s last_recv=%s last_recv_age=%s last_flush=%s "
                "pid=%d ws_instance_id=%s receiver_thread_alive=%s connection_generation=%s "
                "callback_bound=%s registered_count=%s registered_head=%s",
                loop_count,
                ws_state,
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
                os.getpid(),
                snap["instance_id"],
                _thread_alive(getattr(state, "_ws_thread", None)),
                snap["generation"],
                snap["callback_bound"],
                *_registered_head(),
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
                    broken, detail = _registration_is_broken()
                    if broken:
                        # データが来ないのは WebSocket ではなく銘柄登録が通っていないため。
                        # ここで close すると健全な接続を切って再接続ループを作るだけなので、
                        # 本当の原因を出して close はしない。
                        logger.error(
                            "[PUSH MONITOR][WS STALE] force close skipped: REST 銘柄登録が失敗している "
                            "reason=%s %s (WebSocket は健全。kabu Station の register/unregister を確認する)",
                            ws_stale_reason,
                            detail,
                        )
                        ws_stale_count = 0
                    else:
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
