# ============================================================
# File   : core/startup/push_stream_reconnect_stability_patch.py
# Version: V8-PUSH-SAVE-FIRST
# ------------------------------------------------------------
# Purpose:
#   Stabilize kabu Station PUSH WebSocket startup/reconnect.
#
# V8:
#   - PUSH保存数が増えない主因だった short-lived reconnect loop を緩和。
#   - on_open 直後の追加 refresh は原則スキップする。
#     登録は rotation_A/B 側の unregister_all -> wait -> register に一本化。
#   - 短時間切断時の reconnect 待ちを 12s まで膨らませず、最大 3.2s に抑える。
#   - 受信・保存を最優先し、接続直後の二重refreshで kabu Station 側に切られる確率を下げる。
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


def _argv_text() -> str:
    try:
        return " ".join(str(x) for x in sys.argv).lower()
    except Exception:
        return ""


def _is_push_owner_candidate_context() -> bool:
    txt = _argv_text()
    if "db_prepare_runner.py" in txt or "summary_database_runner.py" in txt or "ranking_collector_runner.py" in txt or "yahoo_complement_runner.py" in txt:
        return False
    return "push_receiver_runner.py" in txt or "main.py" in txt


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
    # V8: on_open 直後の二重refreshは kabu Station WS を短命化させやすい。
    # 登録は rotation_A/B の明示ローテーションに任せる。
    _force_env("PUSH_STREAM_SKIP_AFTER_OPEN_REFRESH", "1")
    _force_env("PUSH_STREAM_ONOPEN_REFRESH_CLEAR_FIRST", "0")
    _force_env("PUSH_STREAM_ONOPEN_REFRESH_UNREGISTER_FIRST", "0")
    _force_env("PUSH_STREAM_ONOPEN_REFRESH_WAIT_AFTER_CLEAR", "0.0")
    _force_env("PUSH_STREAM_AFTER_OPEN_REFRESH_DELAY_SEC", "0.50")
    _force_env("PUSH_STREAM_ONOPEN_WS_READY_TIMEOUT_SEC", "1.0")

    # V8: 保存を増やすため、切断後の無受信時間を短くする。
    _force_env("PUSH_STREAM_RECONNECT_BACKOFF_BASE_SEC", "0.8")
    _force_env("PUSH_STREAM_RECONNECT_BACKOFF_MAX_SEC", "3.2")
    _force_env("PUSH_STREAM_RECONNECT_STABLE_RESET_SEC", "10.0")
    _force_env("PUSH_STREAM_SHORT_LIVED_SEC", "4.0")
    _force_env("PUSH_STREAM_SHORT_LIVED_EXTRA_COOLDOWN_SEC", "0.4")

    _setdefault_env("PUSH_STREAM_ONOPEN_REFRESH_THROTTLE", "1")
    _setdefault_env("PUSH_STREAM_ONOPEN_REFRESH_MIN_INTERVAL_SEC", "15")
    _setdefault_env("PUSH_STREAM_ONOPEN_REFRESH_RUNNING_TTL_SEC", "3")

    _setdefault_env("PUSH_STREAM_ONOPEN_REFRESH_FORCE", "0")
    _setdefault_env("PUSH_STREAM_SINGLE_OWNER_LOCK", "1")
    _setdefault_env("PUSH_STREAM_SINGLE_OWNER_WAIT_RETRY", "1")
    _setdefault_env("PUSH_STREAM_SINGLE_OWNER_RETRY_SEC", "2.0")
    _setdefault_env("PUSH_STREAM_SINGLE_OWNER_LOG_EVERY_SEC", "20.0")
    _setdefault_env("PUSH_STREAM_EMPTY_OWNER_LOCK_FAIL_OPEN", "1")
    _setdefault_env("PUSH_STREAM_EMPTY_OWNER_LOCK_REQUIRE_OWNER_CONTEXT", "1")


def _lock_path() -> str:
    return os.getenv("PUSH_STREAM_SINGLE_OWNER_LOCK_PATH") or os.path.join(tempfile.gettempdir(), "autostock_kabustation_push_ws.lock")


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
        logger.warning("[PUSH RECONNECT STABILITY] empty owner PUSH lock detected -> fail-open owner context. path=%s pid=%s argv=%s", path, os.getpid(), sys.argv)
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
    try:
        cur = getattr(transport, "_safe_refresh_subscriptions_after_open", None)
        if getattr(cur, "_push_reconnect_stability_v8", False):
            return True

        def _safe_refresh_subscriptions_after_open_patched() -> None:
            try:
                if _env_bool("PUSH_STREAM_SKIP_AFTER_OPEN_REFRESH", True):
                    logger.warning("[PUSH RECONNECT STABILITY] on_open refresh skipped v8; rotation handles register")
                    return
                delay = max(0.0, _env_float("PUSH_STREAM_AFTER_OPEN_REFRESH_DELAY_SEC", 0.50))
                if delay > 0:
                    time.sleep(delay)
                if not transport._wait_for_ws_ready(timeout=max(0.2, _env_float("PUSH_STREAM_ONOPEN_WS_READY_TIMEOUT_SEC", 1.0))):
                    logger.warning("[push_stream] refresh after open skipped: ws not ready")
                    return
                force = _env_bool("PUSH_STREAM_ONOPEN_REFRESH_FORCE", False)
                logger.warning("[PUSH RECONNECT STABILITY] on_open refresh v8 force=%s delay=%.3f", force, delay)
                transport._call_refresh(force=force, reason="on_open", clear_first=False, unregister_first=False, wait_after_clear=0.0)
            except Exception:
                logger.exception("[push_stream] refresh after open failed")

        _safe_refresh_subscriptions_after_open_patched._push_reconnect_stability_v8 = True  # type: ignore[attr-defined]
        _safe_refresh_subscriptions_after_open_patched._original = cur  # type: ignore[attr-defined]
        transport._safe_refresh_subscriptions_after_open = _safe_refresh_subscriptions_after_open_patched
        logger.warning("[PUSH RECONNECT STABILITY] patched transport on_open refresh skip v8")
        return True
    except Exception:
        logger.exception("[PUSH RECONNECT STABILITY] transport patch failed")
        return False


def _wait_for_single_owner_lock(state: Any, _safe_set_runtime: Any) -> Any:
    retry = max(0.5, _env_float("PUSH_STREAM_SINGLE_OWNER_RETRY_SEC", 2.0))
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
        if getattr(cur, "_push_reconnect_stability_v8", False):
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
            base = max(0.5, _env_float("PUSH_STREAM_RECONNECT_BACKOFF_BASE_SEC", 0.8))
            max_wait = max(base, _env_float("PUSH_STREAM_RECONNECT_BACKOFF_MAX_SEC", 3.2))
            stable_reset = max(3.0, _env_float("PUSH_STREAM_RECONNECT_STABLE_RESET_SEC", 10.0))
            short_lived_sec = max(1.0, _env_float("PUSH_STREAM_SHORT_LIVED_SEC", 4.0))
            short_extra = max(0.0, _env_float("PUSH_STREAM_SHORT_LIVED_EXTRA_COOLDOWN_SEC", 0.4))
            reconnect_wait = base
            logger.info("[push_stream] run loop start url=%s version=%s reconnect_backoff=%.1f..%.1fs stable_reset=%.1fs short_lived=%.1fs extra=%.1fs single_owner=%s lock_mode=%s", ws_url, getattr(runner, "VERSION", "unknown"), base, max_wait, stable_reset, short_lived_sec, short_extra, lock_handle is not None, "locked" if lock_handle is not None else "failopen_or_disabled")
            try:
                while not state._stop_event.is_set():
                    connected_started = time.monotonic()
                    try:
                        _safe_set_runtime("push_stream_running", True)
                        _safe_set_runtime("push_ws_url", ws_url)
                        ws_app = websocket.WebSocketApp(ws_url, on_open=on_open, on_message=on_message, on_error=on_error, on_close=on_close)
                        with state._ws_state_lock:
                            state._ws_app = ws_app
                        logger.info("[push_stream] websocket callbacks wired open=%s message=%s error=%s close=%s", callable(on_open), callable(on_message), callable(on_error), callable(on_close))
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
                    next_wait = min(max_wait, reconnect_wait * 1.6)
                    logger.warning("[push_stream] reconnect after %.1fs lived=%.1fs base_wait=%.1fs next_backoff=%.1fs short_lived=%s", sleep_sec, lived, reconnect_wait, next_wait, lived < short_lived_sec)
                    time.sleep(sleep_sec)
                    reconnect_wait = next_wait
                _safe_set_runtime("push_stream_running", False)
                logger.info("[push_stream] run loop stopped")
            finally:
                _release_single_owner_lock(lock_handle)
                _LOCK_HANDLE = None

        _run_forever_loop_patched._push_reconnect_stability_v8 = True  # type: ignore[attr-defined]
        _run_forever_loop_patched._original = original  # type: ignore[attr-defined]
        runner._run_forever_loop = _run_forever_loop_patched
        logger.warning("[PUSH RECONNECT STABILITY] patched runner v8 save-first single_owner=True wait_retry=True")
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
        logger.warning("[PUSH RECONNECT STABILITY] installed v8 save_first")
        return True
    if retry and not _INSTALLING:
        _INSTALLING = True

        def _loop() -> None:
            global _INSTALLED, _INSTALLING
            try:
                for _ in range(120):
                    if _apply():
                        _INSTALLED = True
                        logger.warning("[PUSH RECONNECT STABILITY] installed v8 by retry save_first")
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
