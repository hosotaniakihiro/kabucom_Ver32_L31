# ============================================================
# File   : core/runtime/stream_orchestrator.py
# Version: Ver24.2-PRODUCTION-PUSH-CALLABLE-INSTALL-NO-SELF-REFRESH
# ------------------------------------------------------------
# ✔ Ver24.1 全機能保持
# ✔ push ws sender resolve/install
# ✔ push refresh callable resolve/install
# ✔ symbol_subscription_manager binding
# ✔ global_data compatibility keep
# ✔ StreamOrchestrator class compatibility restored
# ✔ production hardened
# ✔ NEW: push_stream.refresh_subscriptions を refresh callable に採用しない
# ✔ NEW: symbol_subscription_manager.refresh_subscriptions を優先採用
# ✔ NEW: wrapper callable で self recursion を完全回避
# ============================================================

from __future__ import annotations

import importlib
import logging
import threading
import time
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# ============================================================
# global_data resolve
# ============================================================

try:
    from global_state import global_data
except Exception:  # pragma: no cover
    try:
        from core.global_context.context import global_data  # type: ignore
    except Exception:  # pragma: no cover
        global_data = None


# ============================================================
# module candidates
# ============================================================

_PUSH_STREAM_MODULE_CANDIDATES = [
    "trading.push.push_stream",
]

_SUBSCRIPTION_MANAGER_MODULE_CANDIDATES = [
    "trading.push.symbol_subscription_manager",
]

_REFRESH_CALLABLE_NAME_CANDIDATES = [
    "refresh_subscriptions",
    "_refresh_subscriptions_after_open",
]

_WS_SENDER_NAME_CANDIDATES = [
    "get_ws_sender",
    "send_message",
    "send_raw",
    "_sender",
]

_SET_WS_SENDER_NAME_CANDIDATES = [
    "set_ws_sender",
]

_SET_REFRESH_CALLABLE_NAME_CANDIDATES = [
    "set_refresh_callable",
    "set_refresh_subscriptions",
]

_ORCHESTRATOR_LOCK = threading.RLock()
_LAST_INSTALL_TS = 0.0
_INSTALL_MIN_INTERVAL_SEC = 5.0


# ============================================================
# helpers
# ============================================================

def _import_first(module_names: list[str]) -> Optional[Any]:
    for modname in module_names:
        try:
            return importlib.import_module(modname)
        except Exception:
            continue
    return None


def _get_first_callable(module: Any, names: list[str]) -> Optional[Callable[..., Any]]:
    if module is None:
        return None
    for name in names:
        try:
            fn = getattr(module, name, None)
            if callable(fn):
                return fn
        except Exception:
            continue
    return None


def _safe_setattr(obj: Any, name: str, value: Any) -> bool:
    try:
        setattr(obj, name, value)
        return True
    except Exception:
        logger.exception("❌ setattr failed name=%s", name)
        return False


def _safe_getattr(obj: Any, name: str, default: Any = None) -> Any:
    try:
        return getattr(obj, name, default)
    except Exception:
        return default


def _is_callable_installed(fn: Any) -> bool:
    return callable(fn)


def _call_setter(module: Any, setter_names: list[str], arg: Any, label: str) -> bool:
    if module is None:
        return False

    for name in setter_names:
        setter = getattr(module, name, None)
        if callable(setter):
            try:
                setter(arg)
                logger.info("✅ %s binding installed via %s", label, name)
                return True
            except Exception:
                logger.exception("❌ %s binding failed via %s", label, name)
                return False
    return False


def _resolve_push_stream_module():
    return _import_first(_PUSH_STREAM_MODULE_CANDIDATES)


def _resolve_subscription_manager_module():
    return _import_first(_SUBSCRIPTION_MANAGER_MODULE_CANDIDATES)


def _resolve_ws_sender(push_stream_module: Any) -> Optional[Callable[..., Any]]:
    fn = _get_first_callable(push_stream_module, ["get_ws_sender"])
    if callable(fn):
        try:
            sender = fn()
            if callable(sender):
                logger.info(
                    "✅ push ws sender resolved: %s.get_ws_sender()",
                    getattr(push_stream_module, "__name__", "unknown"),
                )
                return sender
        except Exception:
            logger.exception("❌ push ws sender resolve via get_ws_sender failed")

    fn = _get_first_callable(push_stream_module, _WS_SENDER_NAME_CANDIDATES)
    if fn is not None:
        logger.info(
            "✅ push ws sender resolved: %s.%s",
            getattr(push_stream_module, "__name__", "unknown"),
            getattr(fn, "__name__", str(fn)),
        )
        return fn

    logger.warning("⚠ push ws sender unresolved")
    return None


def _build_subscription_refresh_wrapper(submgr_module: Any) -> Optional[Callable[..., Any]]:
    if submgr_module is None:
        return None

    submgr_refresh = _get_first_callable(submgr_module, ["refresh_subscriptions"])
    if not callable(submgr_refresh):
        return None

    def _refresh_via_subscription_manager(*args, **kwargs):
        return submgr_refresh(*args, **kwargs)

    _refresh_via_subscription_manager.__name__ = "_refresh_via_subscription_manager"
    return _refresh_via_subscription_manager


def _resolve_refresh_callable(push_stream_module: Any, submgr_module: Any) -> Optional[Callable[..., Any]]:
    """
    IMPORTANT:
    push_stream.refresh_subscriptions を push_stream.set_refresh_callable() に
    再注入すると self recursion 防止に弾かれるため採用しない。
    必ず subscription_manager 側を優先する。
    """
    wrapper = _build_subscription_refresh_wrapper(submgr_module)
    if wrapper is not None:
        logger.info(
            "✅ push refresh callable resolved: %s.%s(wrapper)",
            getattr(submgr_module, "__name__", "unknown"),
            "refresh_subscriptions",
        )
        return wrapper

    fn = _get_first_callable(submgr_module, _REFRESH_CALLABLE_NAME_CANDIDATES)
    if fn is not None:
        logger.info(
            "✅ push refresh callable resolved(fallback): %s.%s",
            getattr(submgr_module, "__name__", "unknown"),
            getattr(fn, "__name__", str(fn)),
        )
        return fn

    # push_stream.refresh_subscriptions は self recursion 回避のため採用しない
    logger.warning("⚠ push refresh callable unresolved (subscription manager callable missing)")
    return None


def _install_global_ws_sender(ws_sender: Optional[Callable[..., Any]]) -> bool:
    if global_data is None or ws_sender is None:
        return False

    ok1 = _safe_setattr(global_data, "push_ws_sender", ws_sender)
    ok2 = _safe_setattr(global_data, "ws_sender", ws_sender)

    installed = _is_callable_installed(_safe_getattr(global_data, "push_ws_sender"))
    if installed:
        logger.info("✅ global_data.push_ws_sender installed")
    else:
        logger.warning("⚠ global_data.push_ws_sender not installed")

    return bool(ok1 or ok2)


def _install_global_refresh_callable(refresh_fn: Optional[Callable[..., Any]]) -> bool:
    if global_data is None or refresh_fn is None:
        return False

    ok1 = _safe_setattr(global_data, "push_refresh_callable", refresh_fn)
    ok2 = _safe_setattr(global_data, "refresh_subscriptions", refresh_fn)

    installed = _is_callable_installed(_safe_getattr(global_data, "push_refresh_callable"))
    if installed:
        logger.info("✅ global_data.push_refresh_callable installed")
    else:
        logger.warning("⚠ global_data.push_refresh_callable not installed")

    return bool(ok1 or ok2)


def _install_into_subscription_manager(
    submgr_module: Any,
    ws_sender: Optional[Callable[..., Any]],
    refresh_fn: Optional[Callable[..., Any]],
) -> None:
    if submgr_module is None:
        return

    if ws_sender is not None:
        _call_setter(
            module=submgr_module,
            setter_names=_SET_WS_SENDER_NAME_CANDIDATES,
            arg=ws_sender,
            label="subscription manager ws sender",
        )

    if refresh_fn is not None:
        _call_setter(
            module=submgr_module,
            setter_names=_SET_REFRESH_CALLABLE_NAME_CANDIDATES,
            arg=refresh_fn,
            label="subscription manager refresh callable",
        )


def _install_into_push_stream(
    push_stream_module: Any,
    refresh_fn: Optional[Callable[..., Any]],
) -> None:
    if push_stream_module is None or refresh_fn is None:
        return

    setter_names = _SET_REFRESH_CALLABLE_NAME_CANDIDATES
    installed = _call_setter(
        module=push_stream_module,
        setter_names=setter_names,
        arg=refresh_fn,
        label="push_stream refresh callable",
    )

    if not installed:
        try:
            setattr(push_stream_module, "refresh_subscriptions_callable", refresh_fn)
            logger.info("✅ push_stream refresh callable installed via attribute")
        except Exception:
            logger.exception("❌ push_stream refresh callable attribute install failed")


def _mark_runtime_flags(
    ws_sender: Optional[Callable[..., Any]],
    refresh_fn: Optional[Callable[..., Any]],
) -> None:
    if global_data is None:
        return

    try:
        _safe_setattr(global_data, "push_ws_sender_ready", bool(callable(ws_sender)))
        _safe_setattr(global_data, "push_refresh_ready", bool(callable(refresh_fn)))
        _safe_setattr(
            global_data,
            "subscription_refresh_running",
            False if not callable(refresh_fn) else _safe_getattr(global_data, "subscription_refresh_running", False),
        )
    except Exception:
        logger.exception("❌ runtime flag update failed")


def _should_skip_reinstall(force: bool) -> bool:
    global _LAST_INSTALL_TS

    if force:
        _LAST_INSTALL_TS = time.time()
        return False

    now = time.time()
    if now - _LAST_INSTALL_TS < _INSTALL_MIN_INTERVAL_SEC:
        return True
    _LAST_INSTALL_TS = now
    return False


# ============================================================
# public installer
# ============================================================

def install_push_runtime_bindings(force: bool = False) -> bool:
    with _ORCHESTRATOR_LOCK:
        if _should_skip_reinstall(force=force):
            return True

        push_stream_module = _resolve_push_stream_module()
        submgr_module = _resolve_subscription_manager_module()

        ws_sender = _resolve_ws_sender(push_stream_module)
        refresh_fn = _resolve_refresh_callable(push_stream_module, submgr_module)

        _install_global_ws_sender(ws_sender)
        _install_global_refresh_callable(refresh_fn)

        _install_into_subscription_manager(
            submgr_module=submgr_module,
            ws_sender=ws_sender,
            refresh_fn=refresh_fn,
        )
        _install_into_push_stream(
            push_stream_module=push_stream_module,
            refresh_fn=refresh_fn,
        )
        _mark_runtime_flags(ws_sender=ws_sender, refresh_fn=refresh_fn)

        if ws_sender is None:
            logger.warning("⚠ push ws sender unresolved after install")
        if refresh_fn is None:
            logger.warning("⚠ push refresh callable unresolved after install")

        return bool(ws_sender is not None)


def refresh_push_subscriptions(force: bool = False, reason: str = "runtime") -> bool:
    install_push_runtime_bindings(force=False)

    fn = _safe_getattr(global_data, "push_refresh_callable", None) if global_data is not None else None
    if not callable(fn):
        logger.warning("⚠ global_data.push_refresh_callable not installed")
        return False

    try:
        fn(force=force, reason=reason)
        logger.info("✅ push refresh executed force=%s reason=%s", force, reason)
        return True
    except TypeError:
        try:
            fn(force=force)
            logger.info("✅ push refresh executed(force only) force=%s reason=%s", force, reason)
            return True
        except TypeError:
            try:
                fn()
                logger.info("✅ push refresh executed(no args) reason=%s", reason)
                return True
            except Exception:
                logger.exception("❌ push refresh execution failed(no args)")
                return False
        except Exception:
            logger.exception("❌ push refresh execution failed(force only)")
            return False
    except Exception:
        logger.exception("❌ push refresh execution failed")
        return False


def get_push_ws_sender() -> Optional[Callable[..., Any]]:
    install_push_runtime_bindings(force=False)

    sender = _safe_getattr(global_data, "push_ws_sender", None) if global_data is not None else None
    if callable(sender):
        return sender

    push_stream_module = _resolve_push_stream_module()
    return _resolve_ws_sender(push_stream_module)


def send_push_message(payload: Any) -> bool:
    sender = get_push_ws_sender()
    if not callable(sender):
        logger.warning("⚠ push ws sender unavailable")
        return False

    try:
        sender(payload)
        return True
    except Exception:
        logger.exception("❌ push send failed")
        return False


def on_startup_bindings() -> bool:
    return install_push_runtime_bindings(force=True)


def on_open_refresh() -> bool:
    ok = install_push_runtime_bindings(force=True)
    ok2 = refresh_push_subscriptions(force=True, reason="on_open")
    return bool(ok or ok2)


def periodic_healthcheck_rebind() -> bool:
    return install_push_runtime_bindings(force=False)


# ============================================================
# backward-compatible class
# ============================================================

class StreamOrchestrator:
    """
    Backward-compatible wrapper class.
    """

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.started = False

    def start(self) -> bool:
        ok = on_startup_bindings()
        self.started = bool(ok)
        return self.started

    def stop(self) -> bool:
        self.started = False
        return True

    def run(self) -> bool:
        return self.start()

    def on_startup(self) -> bool:
        return on_startup_bindings()

    def on_open(self) -> bool:
        return on_open_refresh()

    def refresh_subscriptions(self, force: bool = False, reason: str = "runtime") -> bool:
        return refresh_push_subscriptions(force=force, reason=reason)

    def install_bindings(self, force: bool = False) -> bool:
        return install_push_runtime_bindings(force=force)

    def healthcheck(self) -> bool:
        return periodic_healthcheck_rebind()

    def get_ws_sender(self) -> Optional[Callable[..., Any]]:
        return get_push_ws_sender()

    def send_message(self, payload: Any) -> bool:
        return send_push_message(payload)