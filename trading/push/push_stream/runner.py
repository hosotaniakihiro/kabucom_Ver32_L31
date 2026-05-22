# ============================================================
# File   : trading/push/push_stream/runner.py
# Version: Ver1.7-PRODUCTION-PUSH-STREAM-MAIN-MEMORY-ONLY-DB-FLUSH-OFF
# ------------------------------------------------------------
# 【概要】
#   kabu Station PUSH WebSocket runner
#
# 【REV1.7】
#   ✔ main.py / main_database.py 分離運用時、main.py側では
#     push_stream DB writer / order_book writer / flush worker を強制OFF
#   ✔ main.py側は WebSocket受信・latest price cache・5秒足・push_df 更新だけを担当
#   ✔ stream_data へのflushログ/DB保存は main_database.py 側だけに寄せる
#
# 【REV1.6】
#   ✔ enable_rotate=False の memory-only mode でも refresh_callable を設定する
#   ✔ rotation worker OFF でも WebSocket on_open 後の登録refreshを可能にする
#   ✔ 保有銘柄 protected が登録対象に入っても kabuステーションへ送信されない問題を修正
# ============================================================

from __future__ import annotations

import importlib
import logging
import os
import threading
import time
from typing import Any, Callable, Dict, Optional, Tuple

import websocket

from . import state
from .runtime import (
    _ensure_runtime_flags,
    _resolve_ws_url,
    _safe_get_runtime,
    _safe_iso,
    _safe_set_runtime,
)
from .transport import (
    _clear_sender,
    _is_ws_alive,
    refresh_subscriptions,
    set_refresh_callable,
)
from .dataframe import _init_ring_buffer
from .writers import _init_stream_writer, _init_order_book_writer, _flush_worker
from .monitor import _monitor_worker
from .rotation_core import _rotation_worker, enable_rotation
from .ws_callbacks import on_open, on_message, on_error, on_close
from .constants import RECONNECT_WAIT_SEC

logger = logging.getLogger(__name__)

VERSION = "Ver1.7-PRODUCTION-PUSH-STREAM-MAIN-MEMORY-ONLY-DB-FLUSH-OFF"


def _env_bool(name: str, default: bool) -> bool:
    try:
        v = os.environ.get(name)
        if v is None:
            return bool(default)
        s = str(v).strip().lower()
        if s in {"1", "true", "yes", "y", "on"}:
            return True
        if s in {"0", "false", "no", "n", "off"}:
            return False
        return bool(default)
    except Exception:
        return bool(default)


def _should_disable_push_db_write_in_this_process() -> bool:
    """
    main.py側ではpush_streamのDB flush workerを起動しない。

    data_collectors.split_mode.should_skip_data_collector_work_in_main() が True の場合、
    このプロセスは main.py 側なので、PUSH DB保存は main_database.py 側だけに任せる。
    """
    try:
        from data_collectors.split_mode import should_skip_data_collector_work_in_main
        return bool(should_skip_data_collector_work_in_main())
    except Exception:
        return False


def _get_existing_refresh_callable() -> Any:
    try:
        return getattr(state, "_refresh_callable", None)
    except Exception:
        return None


def _is_refresh_callable_alive() -> bool:
    try:
        return callable(_get_existing_refresh_callable())
    except Exception:
        return False


def _is_self_refresh_callable(fn: Any) -> bool:
    try:
        return fn is refresh_subscriptions
    except Exception:
        return False


def _auto_resolve_subscription_refresh_callable() -> Optional[Callable[..., Any]]:
    candidates: Tuple[Tuple[str, str], ...] = (
        ("trading.push.subscription_manager.core", "refresh_subscriptions"),
        ("trading.push.subscription_manager.core", "refresh_subscription_symbols"),
        ("trading.push.subscription_manager.core", "refresh_register_symbols"),
        ("trading.push.subscription_manager", "refresh_subscriptions"),
        ("trading.push.subscription_manager", "refresh_subscription_symbols"),
        ("trading.push.subscription_manager", "refresh_register_symbols"),
        ("trading.push.subscription_manager.core", "register_symbols"),
        ("trading.push.subscription_manager", "register_symbols"),
    )

    for module_name, func_name in candidates:
        try:
            mod = importlib.import_module(module_name)
            fn = getattr(mod, func_name, None)
        except Exception:
            logger.debug(
                "[push_stream] auto resolve refresh callable import/getattr failed module=%s func=%s",
                module_name,
                func_name,
                exc_info=True,
            )
            continue

        if not callable(fn):
            continue

        if _is_self_refresh_callable(fn):
            logger.warning(
                "[push_stream] auto resolve refresh callable skipped self module=%s func=%s",
                module_name,
                func_name,
            )
            continue

        logger.info(
            "[push_stream] auto resolved refresh_callable module=%s func=%s",
            module_name,
            func_name,
        )
        return fn

    logger.warning(
        "[push_stream] auto resolve refresh_callable failed: subscription_manager refresh/register function not found"
    )
    return None


def _set_refresh_callable_preserve(refresh_callable: Optional[Any]) -> bool:
    existing = _get_existing_refresh_callable()

    if refresh_callable is None:
        if callable(existing):
            logger.info(
                "[push_stream] preserve refresh_callable existing=True fn=%s",
                getattr(existing, "__name__", type(existing).__name__),
            )
            return True

        auto_fn = _auto_resolve_subscription_refresh_callable()
        if callable(auto_fn):
            try:
                set_refresh_callable(auto_fn)
                installed = _get_existing_refresh_callable()
                ok = callable(installed)
                logger.info(
                    "[push_stream] refresh_callable auto-installed ok=%s fn=%s",
                    ok,
                    getattr(auto_fn, "__name__", type(auto_fn).__name__),
                )
                return ok
            except Exception:
                logger.exception("[push_stream] auto set_refresh_callable failed")
                return False

        logger.warning("[push_stream] refresh_callable missing and auto resolve failed")
        return False

    if _is_self_refresh_callable(refresh_callable):
        logger.warning(
            "[push_stream] start_push_stream received self refresh callable -> ignore and keep existing existing=%s",
            callable(existing),
        )
        return callable(existing)

    if not callable(refresh_callable):
        logger.warning(
            "[push_stream] start_push_stream refresh_callable rejected: not callable type=%s existing=%s",
            type(refresh_callable).__name__,
            callable(existing),
        )
        return callable(existing)

    try:
        set_refresh_callable(refresh_callable)
        installed = _get_existing_refresh_callable()
        ok = callable(installed)
        logger.info(
            "[push_stream] refresh_callable installed by runner ok=%s fn=%s",
            ok,
            getattr(refresh_callable, "__name__", type(refresh_callable).__name__),
        )
        return ok
    except Exception:
        logger.exception("[push_stream] set_refresh_callable failed; keep existing=%s", callable(existing))
        return callable(existing)


def _set_rotation_preserve(enable_rotate: Optional[bool]) -> bool:
    if enable_rotate is None:
        current = bool(getattr(state, "_rotation_enabled", False))
        logger.info("[push_stream] preserve rotation enabled=%s", current)
        return current

    try:
        enable_rotation(bool(enable_rotate))
    except Exception:
        logger.exception("[push_stream] enable_rotation failed enable_rotate=%s", enable_rotate)

    return bool(getattr(state, "_rotation_enabled", False))


def _start_thread_if_needed(*, attr_name: str, target: Any, name: str) -> threading.Thread:
    th = getattr(state, attr_name, None)
    try:
        if th is not None and th.is_alive():
            logger.info("[push_stream] thread already alive name=%s", name)
            return th
    except Exception:
        pass

    th = threading.Thread(target=target, name=name, daemon=True)
    setattr(state, attr_name, th)
    th.start()
    logger.info("[push_stream] thread started name=%s", name)
    return th


def _sync_runtime_status_after_start() -> None:
    try:
        _safe_set_runtime("push_stream_running", True)
        _safe_set_runtime("push_writer_running", bool(state._flush_thread and state._flush_thread.is_alive()))
        _safe_set_runtime("subscription_refresh_running", _is_refresh_callable_alive())
        _safe_set_runtime("rotation_enabled", bool(getattr(state, "_rotation_enabled", False)))
    except Exception:
        logger.debug("[push_stream] sync runtime status failed", exc_info=True)


def _run_forever_loop() -> None:
    _safe_set_runtime("push_stream_running", True)
    ws_url = _resolve_ws_url()
    logger.info("[push_stream] run loop start url=%s version=%s", ws_url, VERSION)

    while not state._stop_event.is_set():
        try:
            _safe_set_runtime("push_ws_url", ws_url)
            ws_app = websocket.WebSocketApp(
                ws_url,
                on_open=on_open,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close,
            )

            with state._ws_state_lock:
                state._ws_app = ws_app

            logger.info(
                "[push_stream] websocket callbacks wired open=%s message=%s error=%s close=%s",
                callable(on_open),
                callable(on_message),
                callable(on_error),
                callable(on_close),
            )

            try:
                ws_app.run_forever(ping_interval=20, ping_timeout=10, reconnect=0)
            except TypeError:
                ws_app.run_forever(ping_interval=20, ping_timeout=10)

        except Exception:
            logger.exception("[push_stream] run_forever crashed")
        finally:
            try:
                with state._ws_state_lock:
                    state._ws_app = None
            except Exception:
                logger.debug("[push_stream] ws_app clear failed", exc_info=True)

        if state._stop_event.is_set():
            break

        logger.warning("[push_stream] reconnect after %.1fs", RECONNECT_WAIT_SEC)
        time.sleep(RECONNECT_WAIT_SEC)

    _safe_set_runtime("push_stream_running", False)
    logger.info("[push_stream] run loop stopped")


def start_push_stream(
    ws_url: Optional[str] = None,
    stream_writer: Any = None,
    order_book_writer: Any = None,
    refresh_callable: Optional[Any] = None,
    enable_rotate: Optional[bool] = None,
) -> None:
    """PUSH WebSocket をバックグラウンド起動する。"""
    with state._runtime_lock:
        if state._ws_thread is not None and state._ws_thread.is_alive():
            logger.info(
                "[push_stream] already running refresh_callable=%s rotation_enabled=%s status=%s",
                _is_refresh_callable_alive(),
                bool(getattr(state, "_rotation_enabled", False)),
                get_status(),
            )
            return

        if ws_url:
            _safe_set_runtime("push_ws_url", ws_url)

        state._stop_event.clear()
        _ensure_runtime_flags()
        state._ring_buffer = _init_ring_buffer()

        force_memory_only = _should_disable_push_db_write_in_this_process()

        if force_memory_only:
            db_write_enabled = False
            order_book_write_enabled = False
            stream_writer = False
            order_book_writer = False
            _safe_set_runtime("push_stream_memory_only", True)
            _safe_set_runtime("push_stream_db_write_enabled", False)
            _safe_set_runtime("push_stream_order_book_write_enabled", False)
            logger.warning(
                "[push_stream] MAIN MEMORY-ONLY mode: DB writer/order_book writer/flush worker disabled; main_database.py handles PUSH DB storage"
            )
        else:
            db_write_enabled = _env_bool("PUSH_STREAM_DB_WRITE", True) and stream_writer is not False
            order_book_write_enabled = _env_bool("PUSH_STREAM_ORDER_BOOK_WRITE", True) and order_book_writer is not False
            _safe_set_runtime("push_stream_memory_only", False)

        if db_write_enabled:
            state._stream_writer = stream_writer if stream_writer is not None else _init_stream_writer()
        else:
            state._stream_writer = None
            _safe_set_runtime("push_stream_db_write_enabled", False)
            logger.warning("[push_stream] DB write disabled; WebSocket updates memory df/latest cache only")

        if order_book_write_enabled:
            state._order_book_writer = order_book_writer if order_book_writer is not None else _init_order_book_writer()
        else:
            state._order_book_writer = None
            _safe_set_runtime("push_stream_order_book_write_enabled", False)
            logger.warning("[push_stream] order book DB write disabled")

        # 重要:
        # refresh_callable は rotation worker とは別物。
        # enable_rotate=False でも on_open 後の一括登録には refresh_callable が必要。
        refresh_alive = _set_refresh_callable_preserve(refresh_callable)
        rotation_enabled = _set_rotation_preserve(enable_rotate)

        logger.info(
            "[push_stream] start config refresh_callable=%s rotation_enabled=%s enable_rotate_arg=%s db_write=%s order_book_write=%s memory_only=%s version=%s",
            refresh_alive,
            rotation_enabled,
            enable_rotate,
            db_write_enabled,
            order_book_write_enabled,
            force_memory_only,
            VERSION,
        )

        if db_write_enabled:
            _start_thread_if_needed(attr_name="_flush_thread", target=_flush_worker, name="push-flush-worker")
        else:
            state._flush_thread = None
            _safe_set_runtime("push_writer_running", False)
            logger.warning("[push_stream] flush worker not started because db_write_enabled=False")

        _start_thread_if_needed(attr_name="_monitor_thread", target=_monitor_worker, name="push-monitor-worker")

        if rotation_enabled:
            _start_thread_if_needed(attr_name="_rotate_thread", target=_rotation_worker, name="push-rotation-worker")
        else:
            state._rotate_thread = None
            logger.warning("[push_stream] rotation worker not started because rotation_enabled=False")

        _start_thread_if_needed(attr_name="_ws_thread", target=_run_forever_loop, name="push-ws-thread")
        _sync_runtime_status_after_start()

        logger.info(
            "[push_stream] started version=%s refresh_callable=%s rotation_enabled=%s db_write=%s memory_only=%s",
            VERSION,
            _is_refresh_callable_alive(),
            bool(getattr(state, "_rotation_enabled", False)),
            db_write_enabled,
            force_memory_only,
        )


def stop_push_stream(wait: float = 5.0) -> None:
    with state._runtime_lock:
        logger.info("[push_stream] stopping...")
        state._stop_event.set()

        try:
            if state._ws_app is not None:
                state._ws_app.close()
        except Exception:
            logger.exception("[push_stream] ws close failed")

        started = time.time()
        for th in [state._ws_thread, state._flush_thread, state._monitor_thread, state._rotate_thread]:
            try:
                if th is not None and th.is_alive():
                    remain = max(0.1, wait - (time.time() - started))
                    th.join(timeout=remain)
            except Exception:
                logger.exception("[push_stream] thread join failed")

        state._connected_event.clear()
        _clear_sender()
        _safe_set_runtime("ws_connected", False)
        _safe_set_runtime("push_stream_running", False)
        _safe_set_runtime("push_writer_running", False)
        _safe_set_runtime("subscription_refresh_running", False)
        logger.info("[push_stream] stopped")


def get_status() -> Dict[str, Any]:
    return {
        "ws_connected": state._connected_event.is_set(),
        "ws_alive": _is_ws_alive(),
        "push_stream_running": bool(state._ws_thread and state._ws_thread.is_alive()),
        "push_writer_running": bool(state._flush_thread and state._flush_thread.is_alive()),
        "subscription_refresh_running": bool(_safe_get_runtime("subscription_refresh_running", False)),
        "refresh_callable": _is_refresh_callable_alive(),
        "rotation_enabled": bool(getattr(state, "_rotation_enabled", False)),
        "queue_size": state._push_queue.qsize(),
        "df_rows": 0 if state._push_df is None else len(state._push_df),
        "last_push_received_at": _safe_iso(state._last_message_at),
        "last_push_db_flush_at": _safe_iso(state._last_flush_at),
        "last_error_at": _safe_iso(state._last_error_at),
        "last_connect_at": _safe_iso(state._last_connect_at),
        "last_disconnect_at": _safe_iso(state._last_disconnect_at),
        "total_received": state._total_received,
        "total_flushed": state._total_flushed,
        "total_dropped": state._total_dropped,
        "total_errors": state._total_errors,
        "ws_url": _resolve_ws_url(),
        "version": VERSION,
    }


def start(*args: Any, **kwargs: Any) -> None:
    return start_push_stream(*args, **kwargs)


def run_background(*args: Any, **kwargs: Any) -> None:
    return start_push_stream(*args, **kwargs)


__all__ = ["start_push_stream", "stop_push_stream", "get_status", "start", "run_background"]