# ============================================================
# File   : core/startup/push_stream_reconnect_stability_patch.py
# Version: V12-PUSH-VENDOR-ROTATION-CLOSE-REGISTER-RECONNECT
# ------------------------------------------------------------
# Purpose:
#   Stabilize kabu Station PUSH WebSocket startup/reconnect.
#
# Policy:
#   - Do NOT run an extra on_open refresh. A/B rotation owns register.
#   - Keep the requested unregister_all -> 0.2s -> register rotation design.
#   - Match the vendor sample for WebSocket by default: ws.run_forever().
#   - V12: kabu Station may close an existing WebSocket with
#     WinError 10054 / goodbye when subscriptions are changed.
#     Treat that as a vendor reconnect cycle:
#       1) proactively close the WS before rotation REST register
#       2) allow rotation register while WS is closed
#       3) pause WS reconnect while REST register is in progress
#       4) reconnect immediately after register completes
#   - Keep a single PUSH WebSocket owner per PC/process group.
# ============================================================
from __future__ import annotations

import logging
import os
import re
import sys
import tempfile
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)
_INSTALLED = False
_INSTALLING = False
_LOCK_HANDLE: Any = None


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


def _argv_text() -> str:
    try:
        return " ".join(str(x) for x in sys.argv).lower()
    except Exception:
        return ""


def _is_push_owner_candidate_context() -> bool:
    txt = _argv_text()
    if any(x in txt for x in (
        "db_prepare_runner.py",
        "summary_database_runner.py",
        "ranking_collector_runner.py",
        "yahoo_complement_runner.py",
    )):
        return False
    return "push_receiver_runner.py" in txt or "main.py" in txt or "main_database.py" in txt


def _force_env(name: str, value: str) -> None:
    try:
        old = os.environ.get(name)
        os.environ[name] = str(value)
        if old != str(value):
            logger.warning("[PUSH RECONNECT STABILITY] env force %s %s->%s", name, old, value)
    except Exception:
        pass


def _setdefault_env(name: str, value: str) -> None:
    try:
        if os.environ.get(name) is None or str(os.environ.get(name)).strip() == "":
            os.environ[name] = str(value)
    except Exception:
        pass


def _set_default_env() -> None:
    # Save-first: on_open refresh is intentionally disabled.
    _force_env("PUSH_STREAM_SKIP_AFTER_OPEN_REFRESH", "1")
    _force_env("PUSH_STREAM_ONOPEN_REFRESH_CLEAR_FIRST", "0")
    _force_env("PUSH_STREAM_ONOPEN_REFRESH_UNREGISTER_FIRST", "0")
    _force_env("PUSH_STREAM_ONOPEN_REFRESH_WAIT_AFTER_CLEAR", "0.0")
    _force_env("PUSH_STREAM_AFTER_OPEN_REFRESH_DELAY_SEC", "0.50")
    _force_env("PUSH_STREAM_ONOPEN_WS_READY_TIMEOUT_SEC", "1.0")

    # Vendor sample compatibility. kabu Station sample uses ws.run_forever()
    # without ping settings. Keep ping opt-in only.
    _force_env("PUSH_WS_VENDOR_RUN_FOREVER", "1")
    _force_env("PUSH_WS_ENABLE_PING", "0")
    _setdefault_env("PUSH_WS_PING_INTERVAL_SEC", "20")
    _setdefault_env("PUSH_WS_PING_TIMEOUT_SEC", "10")

    # V12: vendor-safe subscription change. Do not keep the WS open while
    # unregister_all/register is executed. The runner will reconnect after REST register.
    _force_env("PUSH_ROTATION_CLOSE_WS_BEFORE_REGISTER", "1")
    _force_env("PUSH_ROTATION_REGISTER_WITH_WS_CLOSED", "1")
    _force_env("PUSH_STREAM_PAUSE_RECONNECT_DURING_REGISTER", "1")
    _force_env("PUSH_ROTATION_WS_CLOSE_SETTLE_SEC", "0.15")

    # kabu Station can reset the WS after a successful register. Keep reconnect gap tiny.
    _force_env("PUSH_STREAM_RECONNECT_BACKOFF_BASE_SEC", "0.3")
    _force_env("PUSH_STREAM_RECONNECT_BACKOFF_MAX_SEC", "1.0")
    _force_env("PUSH_STREAM_RECONNECT_STABLE_RESET_SEC", "5.0")
    _force_env("PUSH_STREAM_SHORT_LIVED_SEC", "1.0")
    _force_env("PUSH_STREAM_SHORT_LIVED_EXTRA_COOLDOWN_SEC", "0.0")

    # Preserve A/B rotation spec.
    _force_env("PUSH_ROTATION_WS_STABLE_GRACE_SEC", "0.0")
    _force_env("PUSH_ROTATION_WS_STABLE_MAX_WAIT_SEC", "1.0")
    _force_env("PUSH_ROTATION_UNREGISTER_WAIT_SEC", "0.2")

    _setdefault_env("PUSH_STREAM_ONOPEN_REFRESH_THROTTLE", "1")
    _setdefault_env("PUSH_STREAM_ONOPEN_REFRESH_MIN_INTERVAL_SEC", "15")
    _setdefault_env("PUSH_STREAM_ONOPEN_REFRESH_RUNNING_TTL_SEC", "3")
    _setdefault_env("PUSH_STREAM_ONOPEN_REFRESH_FORCE", "0")
    _setdefault_env("PUSH_STREAM_SINGLE_OWNER_LOCK", "1")
    _setdefault_env("PUSH_STREAM_SINGLE_OWNER_WAIT_RETRY", "1")
    _setdefault_env("PUSH_STREAM_SINGLE_OWNER_RETRY_SEC", "1.0")
    _setdefault_env("PUSH_STREAM_SINGLE_OWNER_LOG_EVERY_SEC", "20.0")
    _setdefault_env("PUSH_STREAM_EMPTY_OWNER_LOCK_FAIL_OPEN", "1")
    _setdefault_env("PUSH_STREAM_EMPTY_OWNER_LOCK_REQUIRE_OWNER_CONTEXT", "1")


def _lock_path() -> str:
    return os.getenv("PUSH_STREAM_SINGLE_OWNER_LOCK_PATH") or os.path.join(
        tempfile.gettempdir(), "autostock_kabustation_push_ws.lock"
    )


def _parse_owner_pid(text: str) -> int | None:
    try:
        m = re.search(r"pid\s*=\s*(\d+)", text or "")
        return int(m.group(1)) if m else None
    except Exception:
        return None


def _pid_alive(pid: int | None) -> bool | None:
    if not pid or pid <= 0:
        return None
    if pid == os.getpid():
        return True
    try:
        if os.name == "nt":
            import ctypes
            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
            return False
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return None


def _read_lock_text(path: str, fh: Any | None = None) -> str:
    try:
        if fh is not None:
            pos = fh.tell()
            fh.seek(0)
            text = fh.read() or ""
            try:
                fh.seek(pos)
            except Exception:
                pass
            return text
    except Exception:
        pass
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read() or ""
    except Exception:
        return ""


def _empty_owner_lock_should_fail_open(text: str, pid: int | None, alive: bool | None) -> bool:
    try:
        if not _env_bool("PUSH_STREAM_EMPTY_OWNER_LOCK_FAIL_OPEN", True):
            return False
        if pid is not None or alive is not None:
            return False
        if str(text or "").strip() != "":
            return False
        if _env_bool("PUSH_STREAM_EMPTY_OWNER_LOCK_REQUIRE_OWNER_CONTEXT", True) and not _is_push_owner_candidate_context():
            return False
        return True
    except Exception:
        return False


def _lock_conflict_response(path: str, fh: Any, *, log_prefix: str) -> tuple[bool, Any, dict[str, Any]]:
    text = _read_lock_text(path, fh)
    pid = _parse_owner_pid(text)
    alive = _pid_alive(pid)
    if _empty_owner_lock_should_fail_open(text, pid, alive):
        logger.warning(
            "[PUSH RECONNECT STABILITY] empty owner PUSH lock detected -> fail-open owner context. path=%s pid=%s argv=%s",
            path, os.getpid(), sys.argv,
        )
        try:
            fh.close()
        except Exception:
            pass
        return True, None, {"path": path, "empty_owner_failopen": True, "owner_pid": pid, "owner_alive": alive, "text": ""}
    logger.warning("%s path=%s owner_pid=%s owner_alive=%s text=%s -> wait/retry", log_prefix, path, pid, alive, text.strip()[:160])
    try:
        fh.close()
    except Exception:
        pass
    return False, None, {"path": path, "owner_pid": pid, "owner_alive": alive, "text": text.strip()[:200]}


def _try_acquire_single_owner_lock() -> tuple[bool, Any, dict[str, Any]]:
    if not _env_bool("PUSH_STREAM_SINGLE_OWNER_LOCK", True):
        return True, None, {"lock_enabled": False}
    path = _lock_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    except Exception:
        pass
    try:
        fh = open(path, "a+", encoding="utf-8")
    except Exception:
        logger.exception("[PUSH RECONNECT STABILITY] single-owner lock open failed path=%s -> allow", path)
        return True, None, {"path": path, "open_failed": True}
    try:
        if os.name == "nt":
            import msvcrt
            fh.seek(0)
            try:
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                return _lock_conflict_response(path, fh, log_prefix="[PUSH RECONNECT STABILITY] another process owns PUSH WebSocket lock")
        else:
            import fcntl
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return _lock_conflict_response(path, fh, log_prefix="[PUSH RECONNECT STABILITY] another process owns PUSH WebSocket lock")
        fh.seek(0)
        fh.truncate()
        fh.write(f"pid={os.getpid()} started_at={time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        fh.flush()
        logger.warning("[PUSH RECONNECT STABILITY] acquired PUSH WebSocket single-owner lock path=%s pid=%s", path, os.getpid())
        return True, fh, {"path": path, "owner_pid": os.getpid(), "owner_alive": True}
    except Exception:
        logger.exception("[PUSH RECONNECT STABILITY] single-owner lock failed path=%s -> allow", path)
        try:
            fh.close()
        except Exception:
            pass
        return True, None, {"path": path, "lock_failed_allow": True}


def _release_single_owner_lock(fh: Any) -> None:
    if fh is None:
        return
    try:
        if os.name == "nt":
            import msvcrt
            fh.seek(0)
            try:
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            except Exception:
                pass
        else:
            import fcntl
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
    except Exception:
        pass
    try:
        fh.close()
    except Exception:
        pass


def _patch_transport() -> bool:
    try:
        import trading.push.push_stream.transport as transport
    except Exception:
        logger.debug("[PUSH RECONNECT STABILITY] transport not ready", exc_info=True)
        return False
    ok = True
    try:
        cur_after = getattr(transport, "_safe_refresh_subscriptions_after_open", None)
        if not getattr(cur_after, "_push_reconnect_stability_v12", False):
            def _safe_refresh_subscriptions_after_open_patched() -> None:
                logger.warning("[PUSH RECONNECT STABILITY] on_open refresh skipped v12; rotation handles register")
                return

            _safe_refresh_subscriptions_after_open_patched._push_reconnect_stability_v12 = True  # type: ignore[attr-defined]
            _safe_refresh_subscriptions_after_open_patched._original = cur_after  # type: ignore[attr-defined]
            transport._safe_refresh_subscriptions_after_open = _safe_refresh_subscriptions_after_open_patched
            logger.warning("[PUSH RECONNECT STABILITY] patched transport on_open refresh skip v12")
    except Exception:
        logger.exception("[PUSH RECONNECT STABILITY] transport on_open patch failed")
        ok = False

    try:
        cur_call = getattr(transport, "_call_refresh", None)
        if getattr(cur_call, "_push_reconnect_stability_v12", False):
            return ok
        original_call = cur_call

        def _call_refresh_patched(force: bool = True, reason: str = "on_open", **kwargs) -> Any:
            fn = getattr(transport.state, "_refresh_callable", None)
            if not callable(fn):
                transport.logger.info("[push_stream] refresh callable not set -> skip")
                return None

            allow_ws_closed = _is_rotation_reason(reason) and _env_bool("PUSH_ROTATION_REGISTER_WITH_WS_CLOSED", True)
            ws_ready = bool(transport.state._connected_event.is_set() and transport._is_ws_alive())
            if not ws_ready and not allow_ws_closed:
                transport.logger.warning("[push_stream] refresh skipped reason=%s ws_not_ready", reason)
                return None
            if not ws_ready and allow_ws_closed:
                transport.logger.warning("[push_stream] refresh reason=%s allowed with ws_closed for vendor rotation", reason)

            try:
                skip, skip_reason = transport._refresh_recent_or_running(reason)
                if skip:
                    transport.logger.warning("[push_stream] refresh skipped reason=%s guard=%s", reason, skip_reason)
                    transport._safe_set_runtime("subscription_refresh_skip_reason", skip_reason)
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
                transport.logger.info("[push_stream] refresh start reason=%s ws_ready=%s allow_ws_closed=%s kwargs_keys=%s", reason, ws_ready, allow_ws_closed, sorted(list(kwargs.keys())))
                last_type_error: TypeError | None = None
                for label, call_kwargs in attempts:
                    try:
                        transport.logger.info("[push_stream] refresh attempt start reason=%s mode=%s kwargs_keys=%s", reason, label, sorted(list(call_kwargs.keys())))
                        result = fn(**call_kwargs)
                        transport.logger.info("[push_stream] refresh done reason=%s result_type=%s result=%r", reason, type(result).__name__ if result is not None else "NoneType", result)
                        return result
                    except TypeError as e:
                        last_type_error = e
                        transport.logger.warning("[push_stream] refresh attempt TypeError reason=%s mode=%s err=%s -> retry with fewer args", reason, label, e)
                        continue
                if last_type_error is not None:
                    transport.logger.warning("[push_stream] refresh all signatures failed reason=%s last_type_error=%s", reason, last_type_error)
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

        _call_refresh_patched._push_reconnect_stability_v12 = True  # type: ignore[attr-defined]
        _call_refresh_patched._original = original_call  # type: ignore[attr-defined]
        transport._call_refresh = _call_refresh_patched
        logger.warning("[PUSH RECONNECT STABILITY] patched transport rotation refresh allowed with ws_closed v12")
    except Exception:
        logger.exception("[PUSH RECONNECT STABILITY] transport _call_refresh patch failed")
        ok = False
    return ok


def _wait_for_single_owner_lock(state: Any, _safe_set_runtime: Any) -> Any:
    retry = max(0.2, _env_float("PUSH_STREAM_SINGLE_OWNER_RETRY_SEC", 1.0))
    log_every = max(retry, _env_float("PUSH_STREAM_SINGLE_OWNER_LOG_EVERY_SEC", 20.0))
    next_log = 0.0
    while not state._stop_event.is_set():
        ok_lock, lock_handle, detail = _try_acquire_single_owner_lock()
        if ok_lock:
            _safe_set_runtime("push_stream_skipped_reason", "")
            if detail.get("empty_owner_failopen"):
                _safe_set_runtime("push_stream_lock_mode", "empty_owner_failopen")
            return lock_handle
        _safe_set_runtime("push_stream_running", False)
        _safe_set_runtime("push_stream_skipped_reason", "single_owner_lock_held_waiting")
        now = time.monotonic()
        if now >= next_log:
            logger.warning("[push_stream] waiting for PUSH WebSocket single-owner lock retry_sec=%.1f detail=%s", retry, detail)
            next_log = now + log_every
        time.sleep(retry)
    return None


def _run_vendor_forever(ws_app: Any) -> None:
    """Run WebSocketApp in the same style as the official kabu Station sample by default."""
    enable_ping = _env_bool("PUSH_WS_ENABLE_PING", False)
    vendor_mode = _env_bool("PUSH_WS_VENDOR_RUN_FOREVER", True)
    if vendor_mode and not enable_ping:
        logger.warning("[push_stream] run_forever vendor mode: no ping_interval/no reconnect arg")
        return ws_app.run_forever()
    ping_interval = max(0.0, _env_float("PUSH_WS_PING_INTERVAL_SEC", 20.0))
    ping_timeout = max(0.0, _env_float("PUSH_WS_PING_TIMEOUT_SEC", 10.0))
    logger.warning("[push_stream] run_forever ping mode interval=%.1f timeout=%.1f", ping_interval, ping_timeout)
    try:
        return ws_app.run_forever(ping_interval=ping_interval, ping_timeout=ping_timeout, reconnect=0)
    except TypeError:
        return ws_app.run_forever(ping_interval=ping_interval, ping_timeout=ping_timeout)


def _patch_runner() -> bool:
    try:
        import websocket
        import trading.push.push_stream.runner as runner
        from trading.push.push_stream import state
        from trading.push.push_stream.runtime import _resolve_ws_url, _safe_set_runtime
        from trading.push.push_stream.ws_callbacks import on_close, on_error, on_message, on_open
    except Exception:
        logger.debug("[PUSH RECONNECT STABILITY] runner not ready", exc_info=True)
        return False
    try:
        cur = getattr(runner, "_run_forever_loop", None)
        if getattr(cur, "_push_reconnect_stability_v12", False):
            return True
        original = cur

        def _run_forever_loop_patched() -> None:
            global _LOCK_HANDLE
            lock_handle = None
            if _env_bool("PUSH_STREAM_SINGLE_OWNER_WAIT_RETRY", True):
                lock_handle = _wait_for_single_owner_lock(state, _safe_set_runtime)
                if lock_handle is None and state._stop_event.is_set():
                    logger.warning("[push_stream] run loop stopped before acquiring single-owner lock")
                    return
            else:
                ok_lock, lock_handle, detail = _try_acquire_single_owner_lock()
                if not ok_lock:
                    _safe_set_runtime("push_stream_running", False)
                    _safe_set_runtime("push_stream_skipped_reason", "single_owner_lock_held")
                    logger.warning("[push_stream] run loop skipped reason=single_owner_lock_held")
                    return
                if detail.get("empty_owner_failopen"):
                    _safe_set_runtime("push_stream_lock_mode", "empty_owner_failopen")
            _LOCK_HANDLE = lock_handle
            _safe_set_runtime("push_stream_running", True)
            ws_url = _resolve_ws_url()
            base = max(0.1, _env_float("PUSH_STREAM_RECONNECT_BACKOFF_BASE_SEC", 0.3))
            max_wait = max(base, _env_float("PUSH_STREAM_RECONNECT_BACKOFF_MAX_SEC", 1.0))
            fixed_fast = _env_bool("PUSH_STREAM_FAST_FIXED_RECONNECT", True)
            logger.info(
                "[push_stream] run loop start url=%s version=%s reconnect_fast=%.1f..%.1fs fixed=%s single_owner=%s lock_mode=%s vendor_ws=%s ping=%s pause_during_register=%s",
                ws_url, getattr(runner, "VERSION", "unknown"), base, max_wait, fixed_fast,
                lock_handle is not None, "locked" if lock_handle is not None else "failopen_or_disabled",
                _env_bool("PUSH_WS_VENDOR_RUN_FOREVER", True), _env_bool("PUSH_WS_ENABLE_PING", False),
                _env_bool("PUSH_STREAM_PAUSE_RECONNECT_DURING_REGISTER", True),
            )
            try:
                while not state._stop_event.is_set():
                    if _env_bool("PUSH_STREAM_PAUSE_RECONNECT_DURING_REGISTER", True):
                        while bool(getattr(state, "_rotation_register_in_progress", False)) and not state._stop_event.is_set():
                            logger.info("[push_stream] reconnect paused: rotation register in progress")
                            time.sleep(0.1)
                    connected_started = time.monotonic()
                    try:
                        _safe_set_runtime("push_stream_running", True)
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
                            callable(on_open), callable(on_message), callable(on_error), callable(on_close),
                        )
                        _run_vendor_forever(ws_app)
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
                    lived = time.monotonic() - connected_started
                    sleep_sec = base if fixed_fast else min(max_wait, base)
                    logger.warning("[push_stream] reconnect after %.1fs lived=%.1fs fixed_fast=%s vendor_ws=%s", sleep_sec, lived, fixed_fast, _env_bool("PUSH_WS_VENDOR_RUN_FOREVER", True))
                    time.sleep(sleep_sec)
                _safe_set_runtime("push_stream_running", False)
                logger.info("[push_stream] run loop stopped")
            finally:
                _release_single_owner_lock(lock_handle)
                _LOCK_HANDLE = None

        _run_forever_loop_patched._push_reconnect_stability_v12 = True  # type: ignore[attr-defined]
        _run_forever_loop_patched._original = original  # type: ignore[attr-defined]
        runner._run_forever_loop = _run_forever_loop_patched
        logger.warning("[PUSH RECONNECT STABILITY] patched runner v12 vendor-rotation-close-reconnect")
        return True
    except Exception:
        logger.exception("[PUSH RECONNECT STABILITY] runner patch failed")
        return False


def _patch_rotation_core() -> bool:
    try:
        import trading.push.push_stream.rotation_core as rc
        import trading.push.push_stream.transport as transport
        from trading.push.push_stream import state
    except Exception:
        logger.debug("[PUSH RECONNECT STABILITY] rotation_core not ready", exc_info=True)
        return False
    try:
        cur = getattr(rc, "_run_rotation_side", None)
        if getattr(cur, "_push_reconnect_stability_v12", False):
            return True
        original = cur

        def _close_ws_before_rotation_register(label: str) -> None:
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
                transport._clear_sender()
                if ws_app is not None:
                    logger.warning("[push_stream] rotation %s proactively closing WS before REST register to avoid vendor 10054 goodbye", label)
                    try:
                        ws_app.close()
                    except Exception:
                        logger.debug("[push_stream] rotation %s ws close before register failed", label, exc_info=True)
                settle = max(0.0, _env_float("PUSH_ROTATION_WS_CLOSE_SETTLE_SEC", 0.15))
                if settle > 0:
                    time.sleep(settle)
            except Exception:
                logger.exception("[push_stream] rotation %s proactive ws close failed", label)

        def _run_rotation_side_patched(*, label: str, symbols: list[str]) -> bool:
            if state._stop_event.is_set():
                return False
            if not symbols:
                rc.logger.warning("[push_stream] rotation %s skipped: empty symbols", label)
                return False
            reason = f"rotation_{label}"
            try:
                rc.log_register_targets_with_names(symbols, label=label, reason=reason)
            except Exception:
                rc.logger.debug("[push_stream] rotation %s target logging failed", label, exc_info=True)
            try:
                setattr(state, "_rotation_register_in_progress", True)
                rc.logger.warning("[push_stream] rotation %s vendor-safe cycle: close_ws -> REST unregister/register -> reconnect", label)
                _close_ws_before_rotation_register(label)
                ok = rc.run_one_batch_with_timeout(label=label, symbols=symbols, timeout_sec=rc.REGISTER_TIMEOUT_SEC)
            finally:
                setattr(state, "_rotation_register_in_progress", False)
            if not ok:
                rc.logger.warning(
                    "[push_stream] rotation %s register failed -> retry same side without switching size=%d",
                    label,
                    len(symbols),
                )
                rc._sleep_or_stop(1.0)
                return False
            rc.logger.info(
                "[push_stream] rotation %s hold start ok=%s hold=%.3fs size=%d",
                label,
                ok,
                rc.ROTATE_HOLD_SEC,
                len(symbols),
            )
            rc._sleep_or_stop(rc.ROTATE_HOLD_SEC)
            return True

        _run_rotation_side_patched._push_reconnect_stability_v12 = True  # type: ignore[attr-defined]
        _run_rotation_side_patched._original = original  # type: ignore[attr-defined]
        rc._run_rotation_side = _run_rotation_side_patched
        logger.warning("[PUSH RECONNECT STABILITY] patched rotation_core vendor-safe close-before-register v12")
        return True
    except Exception:
        logger.exception("[PUSH RECONNECT STABILITY] rotation_core patch failed")
        return False


def _apply() -> bool:
    _set_default_env()
    ok_transport = _patch_transport()
    ok_runner = _patch_runner()
    ok_rotation = _patch_rotation_core()
    return bool(ok_transport and ok_runner and ok_rotation)


def install(retry: bool = True) -> bool:
    global _INSTALLED, _INSTALLING
    if _INSTALLED:
        return True
    if _apply():
        _INSTALLED = True
        logger.warning("[PUSH RECONNECT STABILITY] installed v12 vendor_rotation_close_register_reconnect")
        return True
    if retry and not _INSTALLING:
        _INSTALLING = True

        def _loop() -> None:
            global _INSTALLED, _INSTALLING
            try:
                for _ in range(120):
                    if _apply():
                        _INSTALLED = True
                        logger.warning("[PUSH RECONNECT STABILITY] installed v12 by retry vendor_rotation_close_register_reconnect")
                        return
                    time.sleep(0.25)
                logger.warning("[PUSH RECONNECT STABILITY] retry exhausted")
            finally:
                _INSTALLING = False

        threading.Thread(target=_loop, name="push-reconnect-stability-install", daemon=True).start()
    return False


try:
    install()
except Exception:
    logger.exception("[PUSH RECONNECT STABILITY] auto install failed")

__all__ = ["install"]
