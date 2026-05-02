# ============================================================
# File   : trading/push/subscription_manager/transport.py
# Function:
#   - ws sender / refresh callable の保持と解決
#   - signature 差異を吸収した互換呼び出し
#   - transport error 判定
#   - transport broken 状態の global_data 反映
# ------------------------------------------------------------
# Notes:
#   - websocket sender 未接続や transport 断を安全に扱う
#   - push_stream 側の mark_ws_broken とも連携可能
# ============================================================

from __future__ import annotations

import inspect
import logging
import time
from typing import Any, Callable, Optional

from .globals_access import safe_get_global_data, safe_getattr, safe_setattr
from . import state

logger = logging.getLogger(__name__)

TRANSPORT_ERROR_NAMES = {
    "ConnectionResetError",
    "BrokenPipeError",
    "ConnectionAbortedError",
    "ConnectionError",
    "WebSocketConnectionClosedException",
    "RuntimeError",
}


# ============================================================
# callable state
# ============================================================

def set_refresh_callable(
    fn: Optional[Callable[..., Any]] = None,
    refresh_callable: Optional[Callable[..., Any]] = None,
    refresher: Optional[Callable[..., Any]] = None,
    refresh_fn: Optional[Callable[..., Any]] = None,
    handler: Optional[Callable[..., Any]] = None,
    **kwargs,
) -> bool:
    del kwargs

    target = fn or refresh_callable or refresher or refresh_fn or handler

    with state.manager_lock:
        state.refresh_callable = target

    logger.info("[SUB MANAGER] set_refresh_callable: %s", bool(target))
    return True


def get_refresh_callable() -> Optional[Callable[..., Any]]:
    with state.manager_lock:
        return state.refresh_callable


def set_ws_sender(
    sender: Optional[Callable[..., Any]] = None,
    ws_sender: Optional[Callable[..., Any]] = None,
    fn: Optional[Callable[..., Any]] = None,
    ws: Optional[Callable[..., Any]] = None,
    ws_callable: Optional[Callable[..., Any]] = None,
    **kwargs,
) -> bool:
    del kwargs

    target = sender or ws_sender or fn or ws or ws_callable

    with state.manager_lock:
        state.ws_sender_cache = target

    logger.info("[SUB MANAGER] set_ws_sender: %s", bool(target))
    return True


def get_ws_sender() -> Optional[Callable[..., Any]]:
    with state.manager_lock:
        return state.ws_sender_cache


# ============================================================
# compatible call
# ============================================================

def signature_accepts_kwargs(fn: Callable[..., Any]) -> bool:
    try:
        sig = inspect.signature(fn)
        for p in sig.parameters.values():
            if p.kind == inspect.Parameter.VAR_KEYWORD:
                return True
    except Exception:
        pass
    return False


def call_compatible(
    fn: Callable[..., Any],
    payload: dict,
    positional_fallback: Optional[list] = None,
) -> Any:
    if fn is None:
        raise RuntimeError("call target is None")

    try:
        sig = inspect.signature(fn)
        if signature_accepts_kwargs(fn):
            return fn(**payload)

        accepted = {}
        for name in sig.parameters.keys():
            if name in payload:
                accepted[name] = payload[name]

        return fn(**accepted)

    except TypeError:
        if positional_fallback is not None:
            return fn(*positional_fallback)
        raise


def is_success_result(result: Any) -> bool:
    if result is None:
        return True
    if isinstance(result, bool):
        return result
    if isinstance(result, dict):
        for key in ("success", "ok", "result", "status"):
            if key in result:
                val = result[key]
                if isinstance(val, bool):
                    return val
                if isinstance(val, str):
                    return val.lower() not in ("error", "failed", "false", "ng")
        if "Code" in result:
            try:
                return int(result["Code"]) == 0
            except Exception:
                return False
        return True
    if isinstance(result, (int, float)):
        return result >= 0
    if isinstance(result, str):
        return result.lower() not in ("error", "failed", "false", "ng")
    return True


# ============================================================
# transport error helpers
# ============================================================

def is_transport_error(exc: BaseException) -> bool:
    name_chain = []
    cur = exc
    visited = 0

    while cur is not None and visited < 10:
        name_chain.append(type(cur).__name__)
        if type(cur).__name__ in TRANSPORT_ERROR_NAMES:
            break
        cur = getattr(cur, "__cause__", None) or getattr(cur, "__context__", None)
        visited += 1

    text = " | ".join(name_chain)

    try:
        msg = str(exc)
    except Exception:
        msg = ""

    merged = f"{text} {msg}".lower()

    tokens = [
        "connection reset",
        "broken pipe",
        "connection aborted",
        "forcibly closed",
        "websocketconnectionclosedexception",
        "winerror 10054",
        "既存の接続はリモート ホストに強制的に切断されました",
        "ws sender not ready",
        "websocket is not connected",
        "sender not connected",
        "not connected",
        "connection closed",
    ]
    return any(t in merged for t in tokens)


def mark_transport_broken(reason: str = "", exc: Optional[BaseException] = None) -> None:
    gd = safe_get_global_data()
    now = time.time()

    if gd is not None:
        safe_setattr(gd, "push_transport_broken", True)
        safe_setattr(gd, "push_transport_broken_reason", reason or "")
        safe_setattr(gd, "push_transport_broken_ts", now)

    try:
        mod = __import__("trading.push.push_stream", fromlist=["mark_ws_broken", "_mark_ws_broken"])
        fn = getattr(mod, "mark_ws_broken", None) or getattr(mod, "_mark_ws_broken", None)

        if callable(fn):
            try:
                fn(reason=reason, exc=exc)
                return
            except TypeError:
                try:
                    fn(reason)
                    return
                except Exception:
                    pass
            except Exception:
                pass
    except Exception:
        pass


def clear_transport_broken_mark() -> None:
    gd = safe_get_global_data()
    if gd is not None:
        safe_setattr(gd, "push_transport_broken", False)
        safe_setattr(gd, "push_transport_broken_reason", "")
        safe_setattr(gd, "push_transport_broken_ts", 0.0)


# ============================================================
# sender / callable resolver
# ============================================================

def resolve_ws_sender(explicit_sender: Optional[Callable[..., Any]] = None) -> Optional[Callable[..., Any]]:
    if callable(explicit_sender):
        return explicit_sender

    cached = get_ws_sender()
    if callable(cached):
        return cached

    gd = safe_get_global_data()
    if gd is not None:
        for attr in (
            "push_ws_sender",
            "ws_sender",
            "send_ws",
            "register_sender",
            "push_register_sender",
        ):
            fn = safe_getattr(gd, attr, None)
            if callable(fn):
                return fn

    try:
        mod = __import__("trading.push.push_stream", fromlist=["get_ws_sender", "ws_sender"])
        fn = getattr(mod, "get_ws_sender", None)
        if callable(fn):
            sender = fn()
            if callable(sender):
                return sender

        sender = getattr(mod, "ws_sender", None)
        if callable(sender):
            return sender
    except Exception:
        pass

    return None


def resolve_refresh_callable(explicit_refresh: Optional[Callable[..., Any]] = None) -> Optional[Callable[..., Any]]:
    if callable(explicit_refresh):
        return explicit_refresh

    cached = get_refresh_callable()
    if callable(cached):
        return cached

    gd = safe_get_global_data()
    if gd is not None:
        for attr in (
            "push_refresh_callable",
            "refresh_subscriptions_callable",
            "subscription_refresh_callable",
        ):
            fn = safe_getattr(gd, attr, None)
            if callable(fn):
                return fn

    return None