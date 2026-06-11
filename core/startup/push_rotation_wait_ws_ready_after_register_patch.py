# ============================================================
# File   : core/startup/push_rotation_wait_ws_ready_after_register_patch.py
# Version: V2-PUSH-ROTATION-WAIT-WS-READY-AFTER-REGISTER-REBIND-REFRESH
# ------------------------------------------------------------
# Purpose:
#   V12 vendor-safe rotation closes the WebSocket before REST
#   unregister_all/register to avoid kabu Station WinError 10054.
#   However, the rotation could start its 4.8s hold before the
#   WebSocket was reconnected, creating a PUSH reception gap.
#
# Policy:
#   - Keep A/B rotation: register -> hold 4.8s -> unregister_all -> 0.2s -> register.
#   - Close WS before REST register remains handled by V12.
#   - After REST register succeeds, wait until WebSocket is actually
#     connected/alive before starting the 4.8s hold.
#   - If WS does not come back within timeout, do not switch side;
#     retry the same A/B side.
#   - V2: rotation_register.py imports transport._call_refresh at import time.
#     Rebind that local reference to the patched ws-closed-aware transport
#     function, otherwise rotation_* still logs ws_not_ready and skips REST register.
# ============================================================
from __future__ import annotations

import logging
import os
import time
from typing import Any

logger = logging.getLogger(__name__)
_INSTALLED = False
_INSTALLING = False


def _env_bool(name: str, default: bool) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        s = str(v).strip().lower()
        if s in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}:
            return True
        if s in {"0", "false", "no", "n", "off", "disable", "disabled"}:
            return False
        return bool(default)
    except Exception:
        return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).replace(",", ""))
    except Exception:
        return float(default)


def _is_rotation_reason(reason: Any) -> bool:
    try:
        s = str(reason or "").strip().lower()
        return s.startswith("rotation_") or s.startswith("push_rotation_") or s in {"rotation", "rotate"}
    except Exception:
        return False


def _force_env(name: str, value: str) -> None:
    try:
        old = os.environ.get(name)
        os.environ[name] = str(value)
        if old != str(value):
            logger.warning("[PUSH ROTATION WAIT WS] env force %s %s->%s", name, old, value)
    except Exception:
        pass


def _set_default_env() -> None:
    # Let runner reconnect immediately after REST register finishes.
    _force_env("PUSH_STREAM_PAUSE_RECONNECT_DURING_REGISTER", "1")
    _force_env("PUSH_STREAM_RECONNECT_BACKOFF_BASE_SEC", "0.3")
    _force_env("PUSH_STREAM_RECONNECT_BACKOFF_MAX_SEC", "1.0")
    # Register is intentionally allowed while WS is closed in vendor-safe rotation.
    _force_env("PUSH_ROTATION_REGISTER_WITH_WS_CLOSED", "1")
    # Wait for actual WS readiness before the 4.8s hold starts.
    _force_env("PUSH_ROTATION_WAIT_WS_READY_AFTER_REGISTER", "1")
    _force_env("PUSH_ROTATION_POST_REGISTER_WS_READY_TIMEOUT_SEC", "4.0")
    _force_env("PUSH_ROTATION_POST_REGISTER_WS_POLL_SEC", "0.05")
    _force_env("PUSH_ROTATION_POST_REGISTER_WS_SETTLE_SEC", "0.25")


def _make_ws_closed_aware_call_refresh(transport: Any):
    def _call_refresh_ws_closed_aware(force: bool = True, reason: str = "on_open", **kwargs) -> Any:
        fn = getattr(transport.state, "_refresh_callable", None)
        if not callable(fn):
            transport.logger.info("[push_stream] refresh callable not set -> skip")
            return None

        allow_ws_closed = _is_rotation_reason(reason) and _env_bool("PUSH_ROTATION_REGISTER_WITH_WS_CLOSED", True)
        try:
            ws_ready = bool(transport.state._connected_event.is_set() and transport._is_ws_alive())
        except Exception:
            ws_ready = False

        if not ws_ready and not allow_ws_closed:
            transport.logger.warning("[push_stream] refresh skipped reason=%s ws_not_ready", reason)
            return None
        if not ws_ready and allow_ws_closed:
            transport.logger.warning(
                "[push_stream] refresh reason=%s allowed with ws_closed for vendor rotation v2",
                reason,
            )

        try:
            skip, skip_reason = transport._refresh_recent_or_running(reason)
            if skip:
                transport.logger.warning("[push_stream] refresh skipped reason=%s guard=%s", reason, skip_reason)
                try:
                    transport._safe_set_runtime("subscription_refresh_skip_reason", skip_reason)
                except Exception:
                    pass
                return True
        except Exception:
            pass

        attempts: list[tuple[str, dict[str, Any]]] = [
            ("full", {"force": force, "reason": reason, **kwargs}),
            ("force_reason", {"force": force, "reason": reason}),
            ("force_only", {"force": force}),
            ("kwargs_only", dict(kwargs)),
            ("none", {}),
        ]
        try:
            transport._mark_refresh_started()
            transport._safe_set_runtime("subscription_refresh_running", True)
            transport.logger.info(
                "[push_stream] refresh start reason=%s ws_ready=%s allow_ws_closed=%s kwargs_keys=%s",
                reason,
                ws_ready,
                allow_ws_closed,
                sorted(list(kwargs.keys())),
            )
            last_type_error: TypeError | None = None
            for label, call_kwargs in attempts:
                try:
                    transport.logger.info(
                        "[push_stream] refresh attempt start reason=%s mode=%s kwargs_keys=%s",
                        reason,
                        label,
                        sorted(list(call_kwargs.keys())),
                    )
                    result = fn(**call_kwargs)
                    transport.logger.info(
                        "[push_stream] refresh done reason=%s result_type=%s result=%r",
                        reason,
                        type(result).__name__ if result is not None else "NoneType",
                        result,
                    )
                    return result
                except TypeError as e:
                    last_type_error = e
                    transport.logger.warning(
                        "[push_stream] refresh attempt TypeError reason=%s mode=%s err=%s -> retry with fewer args",
                        reason,
                        label,
                        e,
                    )
                    continue
            if last_type_error is not None:
                transport.logger.warning(
                    "[push_stream] refresh all signatures failed reason=%s last_type_error=%s",
                    reason,
                    last_type_error,
                )
            return None
        except Exception:
            transport.logger.exception("[push_stream] refresh failed reason=%s", reason)
            return None
        finally:
            try:
                transport._mark_refresh_done()
                transport._safe_set_runtime("subscription_refresh_running", False)
            except Exception:
                pass

    _call_refresh_ws_closed_aware._push_rotation_wait_ws_ready_v2 = True  # type: ignore[attr-defined]
    return _call_refresh_ws_closed_aware


def _patch_rotation_register_refresh_binding() -> bool:
    """rotation_register imported _call_refresh by value, so rebind it after transport is patched."""
    try:
        import trading.push.push_stream.transport as transport
        import trading.push.push_stream.rotation_register as rr
    except Exception:
        logger.debug("[PUSH ROTATION WAIT WS] rotation_register/transport not ready", exc_info=True)
        return False

    try:
        cur_transport_call = getattr(transport, "_call_refresh", None)
        if not getattr(cur_transport_call, "_push_rotation_wait_ws_ready_v2", False):
            cur_transport_call = _make_ws_closed_aware_call_refresh(transport)
            cur_transport_call._original = getattr(transport, "_call_refresh", None)  # type: ignore[attr-defined]
            transport._call_refresh = cur_transport_call
            logger.warning("[PUSH ROTATION WAIT WS] patched transport _call_refresh ws-closed-aware v2")

        cur_rr_call = getattr(rr, "_call_refresh", None)
        if cur_rr_call is not cur_transport_call:
            rr._call_refresh = cur_transport_call
            logger.warning("[PUSH ROTATION WAIT WS] rebound rotation_register._call_refresh to ws-closed-aware transport v2")
        return True
    except Exception:
        logger.exception("[PUSH ROTATION WAIT WS] rotation_register refresh rebind failed")
        return False


def _wait_ws_ready_after_register(label: str, state: Any, transport: Any, rc: Any) -> bool:
    if not _env_bool("PUSH_ROTATION_WAIT_WS_READY_AFTER_REGISTER", True):
        return True
    timeout = max(0.1, _env_float("PUSH_ROTATION_POST_REGISTER_WS_READY_TIMEOUT_SEC", 4.0))
    poll = max(0.02, _env_float("PUSH_ROTATION_POST_REGISTER_WS_POLL_SEC", 0.05))
    deadline = time.monotonic() + timeout
    rc.logger.warning(
        "[push_stream] rotation %s waiting WS ready after REST register timeout=%.3fs",
        label,
        timeout,
    )
    while time.monotonic() < deadline and not state._stop_event.is_set():
        try:
            if state._connected_event.is_set() and transport._is_ws_alive():
                settle = max(0.0, _env_float("PUSH_ROTATION_POST_REGISTER_WS_SETTLE_SEC", 0.25))
                if settle > 0:
                    time.sleep(settle)
                rc.logger.warning(
                    "[push_stream] rotation %s WS ready after REST register -> hold can start settle=%.3fs",
                    label,
                    settle,
                )
                return True
        except Exception:
            pass
        time.sleep(poll)
    rc.logger.warning(
        "[push_stream] rotation %s WS not ready after REST register -> retry same side without switching",
        label,
    )
    return False


def _close_ws_before_rotation_register(label: str, state: Any, transport: Any) -> None:
    if not _env_bool("PUSH_ROTATION_CLOSE_WS_BEFORE_REGISTER", True):
        return
    ws_app = None
    try:
        with state._ws_state_lock:
            ws_app = getattr(state, "_ws_app", None)
    except Exception:
        ws_app = None
    try:
        state._connected_event.clear()
        try:
            transport._clear_sender()
        except Exception:
            pass
        if ws_app is not None:
            logger.warning(
                "[push_stream] rotation %s proactively closing WS before REST register to avoid vendor 10054 goodbye",
                label,
            )
            try:
                ws_app.close()
            except Exception:
                logger.debug("[push_stream] rotation %s ws close before register failed", label, exc_info=True)
        settle = max(0.0, _env_float("PUSH_ROTATION_WS_CLOSE_SETTLE_SEC", 0.15))
        if settle > 0:
            time.sleep(settle)
    except Exception:
        logger.exception("[push_stream] rotation %s proactive ws close failed", label)


def _patch_rotation_core() -> bool:
    try:
        import trading.push.push_stream.rotation_core as rc
        import trading.push.push_stream.transport as transport
        from trading.push.push_stream import state
    except Exception:
        logger.debug("[PUSH ROTATION WAIT WS] rotation_core not ready", exc_info=True)
        return False

    try:
        cur = getattr(rc, "_run_rotation_side", None)
        if getattr(cur, "_push_rotation_wait_ws_ready_v2", False):
            return True
        original = cur

        def _run_rotation_side_wait_ws_ready(*, label: str, symbols: list[str]) -> bool:
            if state._stop_event.is_set():
                return False
            if not symbols:
                rc.logger.warning("[push_stream] rotation %s skipped: empty symbols", label)
                return False

            # Ensure rotation_register uses the ws-closed-aware refresh before dispatch.
            _patch_rotation_register_refresh_binding()

            reason = f"rotation_{label}"
            try:
                rc.log_register_targets_with_names(symbols, label=label, reason=reason)
            except Exception:
                rc.logger.debug("[push_stream] rotation %s target logging failed", label, exc_info=True)

            ok = False
            try:
                setattr(state, "_rotation_register_in_progress", True)
                rc.logger.warning(
                    "[push_stream] rotation %s vendor-safe cycle v14: close_ws -> REST unregister/register -> wait_ws_ready -> hold",
                    label,
                )
                _close_ws_before_rotation_register(label, state, transport)
                ok = rc.run_one_batch_with_timeout(label=label, symbols=symbols, timeout_sec=rc.REGISTER_TIMEOUT_SEC)
            finally:
                # Important: release this flag before waiting for WS; runner reconnect is paused while it is True.
                setattr(state, "_rotation_register_in_progress", False)

            if not ok:
                rc.logger.warning(
                    "[push_stream] rotation %s register failed -> retry same side without switching size=%d",
                    label,
                    len(symbols),
                )
                rc._sleep_or_stop(1.0)
                return False
            if not _wait_ws_ready_after_register(label, state, transport, rc):
                rc._sleep_or_stop(0.5)
                return False

            rc.logger.info(
                "[push_stream] rotation %s hold start ok=%s hold=%.3fs size=%d ws_ready=True",
                label,
                ok,
                rc.ROTATE_HOLD_SEC,
                len(symbols),
            )
            rc._sleep_or_stop(rc.ROTATE_HOLD_SEC)
            return True

        _run_rotation_side_wait_ws_ready._push_rotation_wait_ws_ready_v2 = True  # type: ignore[attr-defined]
        _run_rotation_side_wait_ws_ready._original = original  # type: ignore[attr-defined]
        rc._run_rotation_side = _run_rotation_side_wait_ws_ready
        logger.warning("[PUSH ROTATION WAIT WS] patched rotation_core wait-ws-ready-after-register v2")
        return True
    except Exception:
        logger.exception("[PUSH ROTATION WAIT WS] rotation_core patch failed")
        return False


def _apply() -> bool:
    _set_default_env()
    ok_rebind = _patch_rotation_register_refresh_binding()
    ok_rotation = _patch_rotation_core()
    return bool(ok_rebind and ok_rotation)


def install(retry: bool = True) -> bool:
    global _INSTALLED, _INSTALLING
    if _INSTALLED:
        return True
    if _apply():
        _INSTALLED = True
        logger.warning("[PUSH ROTATION WAIT WS] installed v2 wait_ws_ready_after_register rebind_refresh")
        return True
    if retry and not _INSTALLING:
        _INSTALLING = True

        def _loop() -> None:
            global _INSTALLED, _INSTALLING
            try:
                for _ in range(120):
                    if _apply():
                        _INSTALLED = True
                        logger.warning("[PUSH ROTATION WAIT WS] installed v2 by retry wait_ws_ready_after_register rebind_refresh")
                        return
                    time.sleep(0.25)
                logger.warning("[PUSH ROTATION WAIT WS] retry exhausted")
            finally:
                _INSTALLING = False

        try:
            import threading
            threading.Thread(target=_loop, name="push-rotation-wait-ws-install", daemon=True).start()
        except Exception:
            _INSTALLING = False
    return False


try:
    install()
except Exception:
    logger.exception("[PUSH ROTATION WAIT WS] auto install failed")


__all__ = ["install"]