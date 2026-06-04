# ============================================================
# File   : core/startup/push_stream_reconnect_stability_patch.py
# Version: V3-PUSH-SINGLE-OWNER-WAIT-RETRY
# ------------------------------------------------------------
# Purpose:
#   kabu Station WebSocket can reset connections with WinError 10054
#   when reconnect loops immediately run full unregister/register refresh,
#   or when multiple local processes hold WebSocket clients at once.
#
# Fix:
#   - on_open refresh is skipped by default.
#   - reconnect wait uses exponential backoff with extra cooldown for short-lived connections.
#   - cross-process single-owner file lock prevents competing WebSocket clients.
#   - V3: if the lock is held, do not return permanently. Keep retrying until the
#     owner disappears or this process is stopped. This prevents startup logs like
#       run loop skipped reason=single_owner_lock_held
#     from leaving PUSH dead with total_received=0 for the whole session.
# ============================================================
from __future__ import annotations

import logging
import os
import re
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
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
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


def _set_default_env() -> None:
    # 接続直後の登録更新は既定で停止。登録更新はrotation側に任せる。
    os.environ.setdefault("PUSH_STREAM_SKIP_AFTER_OPEN_REFRESH", "1")
    os.environ.setdefault("PUSH_STREAM_ONOPEN_REFRESH_THROTTLE", "1")
    os.environ.setdefault("PUSH_STREAM_ONOPEN_REFRESH_MIN_INTERVAL_SEC", "60")
    os.environ.setdefault("PUSH_STREAM_ONOPEN_REFRESH_RUNNING_TTL_SEC", "20")
    os.environ.setdefault("PUSH_STREAM_AFTER_OPEN_REFRESH_DELAY_SEC", "8.0")

    # 再接続バックオフ。
    os.environ.setdefault("PUSH_STREAM_RECONNECT_BACKOFF_BASE_SEC", "2.0")
    os.environ.setdefault("PUSH_STREAM_RECONNECT_BACKOFF_MAX_SEC", "60.0")
    os.environ.setdefault("PUSH_STREAM_RECONNECT_STABLE_RESET_SEC", "45.0")
    os.environ.setdefault("PUSH_STREAM_SHORT_LIVED_SEC", "5.0")
    os.environ.setdefault("PUSH_STREAM_SHORT_LIVED_EXTRA_COOLDOWN_SEC", "20.0")

    # on_open refreshは非破壊。
    os.environ.setdefault("PUSH_STREAM_ONOPEN_REFRESH_FORCE", "0")
    os.environ.setdefault("PUSH_STREAM_ONOPEN_REFRESH_CLEAR_FIRST", "0")
    os.environ.setdefault("PUSH_STREAM_ONOPEN_REFRESH_UNREGISTER_FIRST", "0")
    os.environ.setdefault("PUSH_STREAM_ONOPEN_REFRESH_WAIT_AFTER_CLEAR", "0.0")

    # 単一オーナー。ただしV3ではロック中に永久終了しない。
    os.environ.setdefault("PUSH_STREAM_SINGLE_OWNER_LOCK", "1")
    os.environ.setdefault("PUSH_STREAM_SINGLE_OWNER_WAIT_RETRY", "1")
    os.environ.setdefault("PUSH_STREAM_SINGLE_OWNER_RETRY_SEC", "5.0")
    os.environ.setdefault("PUSH_STREAM_SINGLE_OWNER_LOG_EVERY_SEC", "30.0")


def _lock_path() -> str:
    return os.getenv("PUSH_STREAM_SINGLE_OWNER_LOCK_PATH") or os.path.join(
        tempfile.gettempdir(),
        "autostock_kabustation_push_ws.lock",
    )


def _parse_owner_pid(text: str) -> int | None:
    try:
        m = re.search(r"pid\s*=\s*(\d+)", text or "")
        if not m:
            return None
        return int(m.group(1))
    except Exception:
        return None


def _pid_alive(pid: int | None) -> bool | None:
    if not pid or pid <= 0:
        return None
    if pid == os.getpid():
        return True
    try:
        if os.name == "nt":
            # Windows: ctypesでOpenProcessを試す。存在しなければNone/0になる。
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
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
                text = _read_lock_text(path, fh)
                pid = _parse_owner_pid(text)
                alive = _pid_alive(pid)
                logger.warning(
                    "[PUSH RECONNECT STABILITY] another process owns PUSH WebSocket lock path=%s owner_pid=%s owner_alive=%s text=%s -> wait/retry",
                    path,
                    pid,
                    alive,
                    text.strip()[:160],
                )
                try:
                    fh.close()
                except Exception:
                    pass
                return False, None, {"path": path, "owner_pid": pid, "owner_alive": alive, "text": text.strip()[:200]}
        else:
            import fcntl
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                text = _read_lock_text(path, fh)
                pid = _parse_owner_pid(text)
                alive = _pid_alive(pid)
                logger.warning(
                    "[PUSH RECONNECT STABILITY] another process owns PUSH WebSocket lock path=%s owner_pid=%s owner_alive=%s text=%s -> wait/retry",
                    path,
                    pid,
                    alive,
                    text.strip()[:160],
                )
                try:
                    fh.close()
                except Exception:
                    pass
                return False, None, {"path": path, "owner_pid": pid, "owner_alive": alive, "text": text.strip()[:200]}

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
    try:
        cur = getattr(transport, "_safe_refresh_subscriptions_after_open", None)
        if getattr(cur, "_push_reconnect_stability_v3", False):
            return True

        def _safe_refresh_subscriptions_after_open_patched() -> None:
            try:
                if transport._env_bool("PUSH_STREAM_SKIP_AFTER_OPEN_REFRESH", True):
                    logger.warning("[push_stream] refresh after open skipped by env/default PUSH_STREAM_SKIP_AFTER_OPEN_REFRESH=1")
                    return
                delay = max(float(getattr(transport, "AFTER_OPEN_REFRESH_DELAY_SEC", 0.0)), _env_float("PUSH_STREAM_AFTER_OPEN_REFRESH_DELAY_SEC", 8.0))
                time.sleep(delay)
                if not transport._wait_for_ws_ready(timeout=getattr(transport, "WS_READY_WAIT_SEC", 15.0)):
                    logger.warning("[push_stream] refresh after open skipped: ws not ready")
                    return
                force = _env_bool("PUSH_STREAM_ONOPEN_REFRESH_FORCE", False)
                clear_first = _env_bool("PUSH_STREAM_ONOPEN_REFRESH_CLEAR_FIRST", False)
                unregister_first = _env_bool("PUSH_STREAM_ONOPEN_REFRESH_UNREGISTER_FIRST", False)
                wait_after_clear = _env_float("PUSH_STREAM_ONOPEN_REFRESH_WAIT_AFTER_CLEAR", 0.0)
                logger.warning(
                    "[PUSH RECONNECT STABILITY] on_open refresh safe mode force=%s clear_first=%s unregister_first=%s wait_after_clear=%.3f",
                    force,
                    clear_first,
                    unregister_first,
                    wait_after_clear,
                )
                transport._call_refresh(force=force, reason="on_open", clear_first=clear_first, unregister_first=unregister_first, wait_after_clear=wait_after_clear)
            except Exception:
                logger.exception("[push_stream] refresh after open failed")

        _safe_refresh_subscriptions_after_open_patched._push_reconnect_stability_v1 = True  # type: ignore[attr-defined]
        _safe_refresh_subscriptions_after_open_patched._push_reconnect_stability_v2 = True  # type: ignore[attr-defined]
        _safe_refresh_subscriptions_after_open_patched._push_reconnect_stability_v3 = True  # type: ignore[attr-defined]
        _safe_refresh_subscriptions_after_open_patched._original = cur  # type: ignore[attr-defined]
        transport._safe_refresh_subscriptions_after_open = _safe_refresh_subscriptions_after_open_patched
        logger.warning("[PUSH RECONNECT STABILITY] patched transport on_open refresh skipped_by_default=True")
        return True
    except Exception:
        logger.exception("[PUSH RECONNECT STABILITY] transport patch failed")
        return False


def _wait_for_single_owner_lock(state: Any, _safe_set_runtime: Any) -> Any:
    retry = max(1.0, _env_float("PUSH_STREAM_SINGLE_OWNER_RETRY_SEC", 5.0))
    log_every = max(retry, _env_float("PUSH_STREAM_SINGLE_OWNER_LOG_EVERY_SEC", 30.0))
    next_log = 0.0
    while not state._stop_event.is_set():
        ok_lock, lock_handle, detail = _try_acquire_single_owner_lock()
        if ok_lock:
            _safe_set_runtime("push_stream_skipped_reason", "")
            return lock_handle
        _safe_set_runtime("push_stream_running", False)
        _safe_set_runtime("push_stream_skipped_reason", "single_owner_lock_held_waiting")
        now = time.monotonic()
        if now >= next_log:
            logger.warning("[push_stream] waiting for PUSH WebSocket single-owner lock retry_sec=%.1f detail=%s", retry, detail)
            next_log = now + log_every
        time.sleep(retry)
    return None


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
        if getattr(cur, "_push_reconnect_stability_v3", False):
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
                ok_lock, lock_handle, _detail = _try_acquire_single_owner_lock()
                if not ok_lock:
                    _safe_set_runtime("push_stream_running", False)
                    _safe_set_runtime("push_stream_skipped_reason", "single_owner_lock_held")
                    logger.warning("[push_stream] run loop skipped reason=single_owner_lock_held")
                    return
            _LOCK_HANDLE = lock_handle
            _safe_set_runtime("push_stream_running", True)

            ws_url = _resolve_ws_url()
            base = max(0.5, _env_float("PUSH_STREAM_RECONNECT_BACKOFF_BASE_SEC", 2.0))
            max_wait = max(base, _env_float("PUSH_STREAM_RECONNECT_BACKOFF_MAX_SEC", 60.0))
            stable_reset = max(5.0, _env_float("PUSH_STREAM_RECONNECT_STABLE_RESET_SEC", 45.0))
            short_lived_sec = max(1.0, _env_float("PUSH_STREAM_SHORT_LIVED_SEC", 5.0))
            short_extra = max(0.0, _env_float("PUSH_STREAM_SHORT_LIVED_EXTRA_COOLDOWN_SEC", 20.0))
            reconnect_wait = base
            logger.info(
                "[push_stream] run loop start url=%s version=%s reconnect_backoff=%.1f..%.1fs stable_reset=%.1fs short_lived=%.1fs extra=%.1fs single_owner=%s",
                ws_url,
                getattr(runner, "VERSION", "unknown"),
                base,
                max_wait,
                stable_reset,
                short_lived_sec,
                short_extra,
                lock_handle is not None,
            )

            try:
                while not state._stop_event.is_set():
                    connected_started = time.monotonic()
                    try:
                        _safe_set_runtime("push_stream_running", True)
                        _safe_set_runtime("push_ws_url", ws_url)
                        ws_app = websocket.WebSocketApp(ws_url, on_open=on_open, on_message=on_message, on_error=on_error, on_close=on_close)
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
                    lived = time.monotonic() - connected_started
                    if lived >= stable_reset:
                        reconnect_wait = base
                    sleep_sec = reconnect_wait
                    if lived < short_lived_sec:
                        sleep_sec = min(max_wait, sleep_sec + short_extra)
                    next_wait = min(max_wait, reconnect_wait * 2.0)
                    logger.warning(
                        "[push_stream] reconnect after %.1fs lived=%.1fs base_wait=%.1fs next_backoff=%.1fs short_lived=%s",
                        sleep_sec,
                        lived,
                        reconnect_wait,
                        next_wait,
                        lived < short_lived_sec,
                    )
                    time.sleep(sleep_sec)
                    reconnect_wait = next_wait
                _safe_set_runtime("push_stream_running", False)
                logger.info("[push_stream] run loop stopped")
            finally:
                _release_single_owner_lock(lock_handle)
                _LOCK_HANDLE = None

        _run_forever_loop_patched._push_reconnect_stability_v1 = True  # type: ignore[attr-defined]
        _run_forever_loop_patched._push_reconnect_stability_v2 = True  # type: ignore[attr-defined]
        _run_forever_loop_patched._push_reconnect_stability_v3 = True  # type: ignore[attr-defined]
        _run_forever_loop_patched._original = original  # type: ignore[attr-defined]
        runner._run_forever_loop = _run_forever_loop_patched
        logger.warning("[PUSH RECONNECT STABILITY] patched runner reconnect cooldown=True single_owner=True wait_retry=True")
        return True
    except Exception:
        logger.exception("[PUSH RECONNECT STABILITY] runner patch failed")
        return False


def _apply() -> bool:
    _set_default_env()
    ok_transport = _patch_transport()
    ok_runner = _patch_runner()
    return bool(ok_transport and ok_runner)


def install(retry: bool = True) -> bool:
    global _INSTALLED, _INSTALLING
    if _INSTALLED:
        return True
    if _apply():
        _INSTALLED = True
        logger.warning("[PUSH RECONNECT STABILITY] installed v3")
        return True
    if retry and not _INSTALLING:
        _INSTALLING = True

        def _loop() -> None:
            global _INSTALLED, _INSTALLING
            try:
                for _ in range(120):
                    if _apply():
                        _INSTALLED = True
                        logger.warning("[PUSH RECONNECT STABILITY] installed v3 by retry")
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
