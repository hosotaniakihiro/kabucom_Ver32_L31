# ============================================================
# File   : trading/push/push_stream/transport.py
# Version: Ver1.6-PUSH-STREAM-TRANSPORT-ROTATION-WS-CLOSED-AWARE
# ------------------------------------------------------------
# WebSocket transport helpers.
#
# REV1.6:
#   - rotation_A / rotation_B registration is REST based, not WS-send based.
#   - Therefore rotation refresh may run while WebSocket is intentionally closed.
#   - This integrates the former startup monkey-patch behavior into the core module.
# ============================================================

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Callable, Optional

import websocket
from websocket._exceptions import WebSocketConnectionClosedException

from .constants import AFTER_OPEN_REFRESH_DELAY_SEC, WS_READY_POLL_SEC, WS_READY_WAIT_SEC
from . import state
from .runtime import _now, _safe_set_runtime

logger = logging.getLogger(__name__)

VERSION = "Ver1.6-PUSH-STREAM-TRANSPORT-ROTATION-WS-CLOSED-AWARE"

_last_refresh_started_ts = 0.0
_last_refresh_done_ts = 0.0
_refresh_guard_lock = threading.Lock()


def _env_bool(name: str, default: bool = False) -> bool:
    try:
        v = os.environ.get(name)
        if v is None:
            return bool(default)
        s = str(v).strip().lower()
        if s in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}:
            return True
        if s in {"0", "false", "no", "n", "off", "ng", "disable", "disabled", ""}:
            return False
        return bool(default)
    except Exception:
        return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.environ.get(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _is_rotation_reason(reason: str) -> bool:
    return str(reason or "").startswith("rotation_")


def _allow_refresh_with_ws_closed(reason: str) -> bool:
    return _is_rotation_reason(reason) and _env_bool("PUSH_ROTATION_REGISTER_WITH_WS_CLOSED", True)


# ============================================================
# ws state
# ============================================================

def _get_ws_app() -> Optional[websocket.WebSocketApp]:
    with state._ws_state_lock:
        return state._ws_app


def _is_ws_alive(ws: Optional[websocket.WebSocketApp] = None) -> bool:
    try:
        target = ws if ws is not None else _get_ws_app()
        if target is None:
            return False
        sock = getattr(target, "sock", None)
        if sock is None:
            return False
        return bool(getattr(sock, "connected", False))
    except Exception:
        return False


def _clear_sender() -> None:
    with state._sender_lock:
        state._ws_sender = None
    logger.info("[push_stream] ws sender cleared")


def _mark_ws_disconnected(reason: str = "unknown") -> None:
    state._last_disconnect_at = _now()
    state._connected_event.clear()
    _safe_set_runtime("ws_connected", False)
    logger.warning("[push_stream] mark ws disconnected reason=%s", reason)
    _clear_sender()


def _install_sender(ws: websocket.WebSocketApp) -> None:
    with state._sender_lock:

        def _sender(raw: str) -> Any:
            if ws is None:
                _mark_ws_disconnected("sender_ws_app_unavailable")
                raise RuntimeError("ws app unavailable")

            sock = getattr(ws, "sock", None)
            if sock is None or not getattr(sock, "connected", False):
                _mark_ws_disconnected("sender_socket_not_connected")
                raise RuntimeError("ws socket not connected")

            try:
                return ws.send(raw)
            except WebSocketConnectionClosedException as e:
                _mark_ws_disconnected("sender_websocket_closed")
                raise RuntimeError("ws send failed: websocket closed") from e
            except ConnectionResetError as e:
                _mark_ws_disconnected("sender_connection_reset")
                raise RuntimeError("ws send failed: connection reset") from e
            except BrokenPipeError as e:
                _mark_ws_disconnected("sender_broken_pipe")
                raise RuntimeError("ws send failed: broken pipe") from e
            except OSError as e:
                _mark_ws_disconnected(f"sender_os_error:{getattr(e, 'winerror', None)}")
                raise RuntimeError(f"ws send failed: os error {e}") from e

        state._ws_sender = _sender

    logger.info("[push_stream] ws sender installed callable=%s", callable(state._ws_sender))


def get_ws_sender() -> Optional[Callable[[str], Any]]:
    with state._sender_lock:
        return state._ws_sender


def _wait_for_ws_ready(timeout: float = WS_READY_WAIT_SEC) -> bool:
    deadline = time.time() + max(0.1, float(timeout))
    while time.time() < deadline and not state._stop_event.is_set():
        if state._connected_event.is_set() and _is_ws_alive():
            return True
        time.sleep(WS_READY_POLL_SEC)
    return state._connected_event.is_set() and _is_ws_alive()


# ============================================================
# refresh callable
# ============================================================

def set_refresh_callable(fn: Optional[Callable[..., Any]]) -> None:
    if fn is None:
        state._refresh_callable = None
        logger.info("[push_stream] set_refresh_callable: False")
        return
    if not callable(fn):
        logger.warning("[push_stream] set_refresh_callable rejected: not callable")
        return
    if fn is refresh_subscriptions:
        logger.warning("[push_stream] rejected self refresh callable: refresh_subscriptions (keep existing)")
        return
    state._refresh_callable = fn
    logger.info("[push_stream] set_refresh_callable: True fn=%s", getattr(fn, "__name__", type(fn).__name__))


def _refresh_recent_or_running(reason: str) -> tuple[bool, str]:
    """Throttle only on_open refresh storms. rotation_* must not be throttled here."""
    if reason != "on_open":
        return False, "not_on_open"
    if not _env_bool("PUSH_STREAM_ONOPEN_REFRESH_THROTTLE", True):
        return False, "disabled"
    now = time.monotonic()
    min_interval = max(1.0, _env_float("PUSH_STREAM_ONOPEN_REFRESH_MIN_INTERVAL_SEC", 15.0))
    running_ttl = max(2.0, _env_float("PUSH_STREAM_ONOPEN_REFRESH_RUNNING_TTL_SEC", 8.0))
    with _refresh_guard_lock:
        since_start = now - float(_last_refresh_started_ts or 0.0)
        since_done = now - float(_last_refresh_done_ts or 0.0)
        if _last_refresh_started_ts and since_start < running_ttl and (_last_refresh_done_ts < _last_refresh_started_ts):
            return True, f"refresh_running since_start={since_start:.1f}s ttl={running_ttl:.1f}s"
        if _last_refresh_done_ts and since_done < min_interval:
            return True, f"recent_refresh since_done={since_done:.1f}s min_interval={min_interval:.1f}s"
    return False, "ok"


def _mark_refresh_started() -> None:
    global _last_refresh_started_ts
    with _refresh_guard_lock:
        _last_refresh_started_ts = time.monotonic()


def _mark_refresh_done() -> None:
    global _last_refresh_done_ts
    with _refresh_guard_lock:
        _last_refresh_done_ts = time.monotonic()


def _invoke_refresh_callable(
    fn: Callable[..., Any],
    *,
    force: bool,
    reason: str,
    kwargs: dict[str, Any],
    allow_ws_closed: bool = False,
) -> Any:
    attempts: list[tuple[str, dict[str, Any]]] = [
        ("full", {"force": force, "reason": reason, **kwargs}),
        ("force_reason", {"force": force, "reason": reason}),
        ("force_only", {"force": force}),
        ("kwargs_only", dict(kwargs)),
        ("none", {}),
    ]
    last_type_error: TypeError | None = None

    for label, call_kwargs in attempts:
        if not allow_ws_closed and (not state._connected_event.is_set() or not _is_ws_alive()):
            logger.warning("[push_stream] refresh attempt skipped reason=%s mode=%s ws_not_ready", reason, label)
            return None
        try:
            logger.info(
                "[push_stream] refresh attempt start reason=%s mode=%s allow_ws_closed=%s kwargs_keys=%s",
                reason,
                label,
                allow_ws_closed,
                sorted(list(call_kwargs.keys())),
            )
            return fn(**call_kwargs)
        except TypeError as e:
            last_type_error = e
            logger.warning(
                "[push_stream] refresh attempt TypeError reason=%s mode=%s err=%s -> retry with fewer args",
                reason,
                label,
                e,
            )
            continue

    if last_type_error is not None:
        logger.warning("[push_stream] refresh all signatures failed reason=%s last_type_error=%s", reason, last_type_error)
    return None


def _call_refresh(force: bool = True, reason: str = "on_open", **kwargs) -> Any:
    fn = state._refresh_callable
    reason_s = str(reason or "")
    allow_ws_closed = _allow_refresh_with_ws_closed(reason_s)

    if not callable(fn):
        logger.info("[push_stream] refresh callable not set -> skip")
        return None

    if not allow_ws_closed and (not state._connected_event.is_set() or not _is_ws_alive()):
        logger.warning("[push_stream] refresh skipped reason=%s ws_not_ready", reason_s)
        return None

    skip, skip_reason = _refresh_recent_or_running(reason_s)
    if skip:
        logger.warning("[push_stream] refresh skipped reason=%s guard=%s", reason_s, skip_reason)
        _safe_set_runtime("subscription_refresh_skip_reason", skip_reason)
        return True

    try:
        _mark_refresh_started()
        _safe_set_runtime("subscription_refresh_running", True)
        if allow_ws_closed:
            logger.warning(
                "[push_stream] refresh reason=%s allowed with ws_closed for REST rotation kwargs_keys=%s",
                reason_s,
                sorted(list(kwargs.keys())),
            )
        else:
            logger.info("[push_stream] refresh start reason=%s kwargs_keys=%s", reason_s, sorted(list(kwargs.keys())))
        result = _invoke_refresh_callable(
            fn,
            force=force,
            reason=reason_s,
            kwargs=dict(kwargs),
            allow_ws_closed=allow_ws_closed,
        )
        logger.info(
            "[push_stream] refresh done reason=%s result_type=%s result=%r",
            reason_s,
            type(result).__name__ if result is not None else "NoneType",
            result,
        )
        return result
    except Exception:
        logger.exception("[push_stream] refresh failed reason=%s", reason_s)
        return None
    finally:
        _mark_refresh_done()
        _safe_set_runtime("subscription_refresh_running", False)


def _safe_refresh_subscriptions_after_open() -> None:
    try:
        if _env_bool("PUSH_STREAM_SKIP_AFTER_OPEN_REFRESH", False):
            logger.warning("[push_stream] refresh after open skipped by env PUSH_STREAM_SKIP_AFTER_OPEN_REFRESH=1")
            return
        delay = max(float(AFTER_OPEN_REFRESH_DELAY_SEC), _env_float("PUSH_STREAM_AFTER_OPEN_REFRESH_DELAY_SEC", 2.0))
        time.sleep(delay)
        if not _wait_for_ws_ready(timeout=WS_READY_WAIT_SEC):
            logger.warning("[push_stream] refresh after open skipped: ws not ready")
            return
        _call_refresh(
            force=True,
            reason="on_open",
            clear_first=True,
            unregister_first=True,
            wait_after_clear=0.5,
        )
    except Exception:
        logger.exception("[push_stream] refresh after open failed")


def refresh_subscriptions(*args, **kwargs) -> Any:
    fn = state._refresh_callable
    if not callable(fn):
        logger.info("[push_stream] refresh_subscriptions skipped (callable missing)")
        return None
    if fn is refresh_subscriptions:
        logger.error("[push_stream] refresh_subscriptions recursion blocked: fn is self")
        return None
    if not state._connected_event.is_set() or not _is_ws_alive():
        logger.warning("[push_stream] refresh_subscriptions skipped: ws_not_ready")
        return None
    try:
        _safe_set_runtime("subscription_refresh_running", True)
        result = fn(*args, **kwargs)
        logger.info(
            "[push_stream] refresh_subscriptions done result_type=%s result=%r",
            type(result).__name__ if result is not None else "NoneType",
            result,
        )
        return result
    except TypeError as e:
        logger.warning("[push_stream] refresh_subscriptions TypeError err=%s -> retry safe no-arg", e)
        try:
            result = fn()
            logger.info(
                "[push_stream] refresh_subscriptions no-arg done result_type=%s result=%r",
                type(result).__name__ if result is not None else "NoneType",
                result,
            )
            return result
        except Exception:
            logger.exception("[push_stream] refresh_subscriptions no-arg retry failed")
            return None
    except Exception:
        logger.exception("[push_stream] refresh_subscriptions failed")
        return None
    finally:
        _safe_set_runtime("subscription_refresh_running", False)


# ============================================================
# public status
# ============================================================

def wait_until_connected(timeout: float = 15.0) -> bool:
    return state._connected_event.wait(timeout=timeout) and _is_ws_alive()


def is_connected() -> bool:
    return state._connected_event.is_set() and _is_ws_alive()


def _start_refresh_after_open_thread() -> None:
    if _env_bool("PUSH_STREAM_SKIP_AFTER_OPEN_REFRESH", False):
        logger.warning("[push_stream] refresh after open thread not started by env PUSH_STREAM_SKIP_AFTER_OPEN_REFRESH=1")
        return
    threading.Thread(
        target=_safe_refresh_subscriptions_after_open,
        name="push-refresh-after-open",
        daemon=True,
    ).start()


__all__ = [
    "VERSION",
    "_get_ws_app",
    "_is_ws_alive",
    "_clear_sender",
    "_install_sender",
    "get_ws_sender",
    "_mark_ws_disconnected",
    "_wait_for_ws_ready",
    "set_refresh_callable",
    "_call_refresh",
    "refresh_subscriptions",
    "wait_until_connected",
    "is_connected",
    "_start_refresh_after_open_thread",
]
