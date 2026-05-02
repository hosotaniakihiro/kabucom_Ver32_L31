# ============================================================
# File   : trading/push/push_stream/transport.py
# Version: Ver1.2-PUSH-STREAM-TRANSPORT-REFRESH-RESULT-STRICT
# ------------------------------------------------------------
# ✔ WebSocket sender install / clear
# ✔ ws alive 判定
# ✔ refresh callable 管理
# ✔ on_open 後 refresh
# ✔ ConnectionResetError / BrokenPipeError / OSError を安全処理
# ✔ ws.send 失敗時に connected_event clear + sender clear
# ✔ register_symbols 側へ RuntimeError として安全伝搬
# ✔ refresh の戻り値 / result_type / kwargs を詳細ログ
# ✔ refresh 空振りの可視化強化
# ============================================================

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Optional

import websocket
from websocket._exceptions import WebSocketConnectionClosedException

from .constants import AFTER_OPEN_REFRESH_DELAY_SEC, WS_READY_POLL_SEC, WS_READY_WAIT_SEC
from . import state
from .runtime import _now, _safe_set_runtime

logger = logging.getLogger(__name__)


# ============================================================
# ws state
# ============================================================

def _get_ws_app() -> Optional[websocket.WebSocketApp]:
    with state._ws_state_lock:
        return state._ws_app


def _is_ws_alive(ws: Optional[websocket.WebSocketApp] = None) -> bool:
    """
    WebSocketApp が送信可能そうか確認する。

    注意:
      - sock.connected=True でも send 直後に WinError 10054 になることがある
      - ここは事前判定であり、send 失敗は _sender 側で最終捕捉する
    """
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
    """
    WebSocket 切断状態を runtime / event / sender に反映する。
    """
    state._last_disconnect_at = _now()
    state._connected_event.clear()
    _safe_set_runtime("ws_connected", False)
    logger.warning("[push_stream] mark ws disconnected reason=%s", reason)
    _clear_sender()


def _install_sender(ws: websocket.WebSocketApp) -> None:
    """
    ws.send を安全に包んだ sender を state に登録する。

    重要:
      - ws.send 中に WinError 10054 / ConnectionResetError が出ることがある
      - その場合は sender を clear し、connected_event を落とす
      - 上位には RuntimeError として返し、rotation 側で warning 扱いにする
    """
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
        logger.warning(
            "[push_stream] rejected self refresh callable: refresh_subscriptions (keep existing)"
        )
        return

    state._refresh_callable = fn
    logger.info(
        "[push_stream] set_refresh_callable: True fn=%s",
        getattr(fn, "__name__", type(fn).__name__),
    )


def _call_refresh(force: bool = True, reason: str = "on_open", **kwargs) -> Any:
    fn = state._refresh_callable

    if not callable(fn):
        logger.info("[push_stream] refresh callable not set -> skip")
        return None

    if not state._connected_event.is_set() or not _is_ws_alive():
        logger.warning("[push_stream] refresh skipped reason=%s ws_not_ready", reason)
        return None

    try:
        _safe_set_runtime("subscription_refresh_running", True)
        logger.info(
            "[push_stream] refresh start reason=%s kwargs_keys=%s",
            reason,
            sorted(list(kwargs.keys())),
        )
        result = fn(force=force, reason=reason, **kwargs)
        logger.info(
            "[push_stream] refresh done reason=%s result_type=%s result=%r",
            reason,
            type(result).__name__ if result is not None else "NoneType",
            result,
        )
        return result

    except TypeError:
        try:
            if not state._connected_event.is_set() or not _is_ws_alive():
                logger.warning("[push_stream] refresh legacy skipped reason=%s ws_not_ready", reason)
                return None

            logger.info(
                "[push_stream] refresh legacy start reason=%s kwargs_keys=%s",
                reason,
                sorted(list(kwargs.keys())),
            )
            result = fn(**kwargs)
            logger.info(
                "[push_stream] refresh done reason=%s legacy result_type=%s result=%r",
                reason,
                type(result).__name__ if result is not None else "NoneType",
                result,
            )
            return result

        except Exception:
            logger.exception("[push_stream] refresh legacy call failed reason=%s", reason)
            return None

    except Exception:
        logger.exception("[push_stream] refresh failed reason=%s", reason)
        return None

    finally:
        _safe_set_runtime("subscription_refresh_running", False)


def _safe_refresh_subscriptions_after_open() -> None:
    try:
        time.sleep(AFTER_OPEN_REFRESH_DELAY_SEC)

        if not _wait_for_ws_ready(timeout=WS_READY_WAIT_SEC):
            logger.warning("[push_stream] refresh after open skipped: ws not ready")
            return

        _call_refresh(force=True, reason="on_open")

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
    threading.Thread(
        target=_safe_refresh_subscriptions_after_open,
        name="push-refresh-after-open",
        daemon=True,
    ).start()


__all__ = [
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