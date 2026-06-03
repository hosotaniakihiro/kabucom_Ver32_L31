# ============================================================
# File   : core/startup/push_stream_reconnect_stability_patch.py
# Version: V2-PUSH-RECONNECT-STABILITY-SINGLE-OWNER-COOLDOWN
# ------------------------------------------------------------
# Purpose:
#   kabu Station WebSocket can reset connections with WinError 10054
#   when reconnect loops immediately run full unregister/register refresh,
#   or when multiple local processes hold WebSocket clients at once.
#
# Fix:
#   - on_open refresh is skipped by default. Rotation/register code handles
#     subscription updates after the connection is alive.
#   - If explicitly enabled, on_open refresh remains non-destructive:
#       force=False / clear_first=False / unregister_first=False
#   - reconnect wait uses exponential backoff with a longer max.
#   - short-lived connections trigger extra cooldown.
#   - a cross-process single-owner file lock prevents main.py and
#     main_database.py from opening competing WebSocket clients.
# ============================================================
from __future__ import annotations

import logging
import os
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
    # on_open直後の登録更新は既定で完全停止。
    # 接続直後1〜3秒で切れる状態では、refresh threadを起こすだけでもノイズになる。
    os.environ.setdefault("PUSH_STREAM_SKIP_AFTER_OPEN_REFRESH", "1")
    os.environ.setdefault("PUSH_STREAM_ONOPEN_REFRESH_THROTTLE", "1")
    os.environ.setdefault("PUSH_STREAM_ONOPEN_REFRESH_MIN_INTERVAL_SEC", "60")
    os.environ.setdefault("PUSH_STREAM_ONOPEN_REFRESH_RUNNING_TTL_SEC", "20")
    os.environ.setdefault("PUSH_STREAM_AFTER_OPEN_REFRESH_DELAY_SEC", "8.0")

    # 再接続は0.5秒固定ではなく、短命接続時は長めに冷却する。
    os.environ.setdefault("PUSH_STREAM_RECONNECT_BACKOFF_BASE_SEC", "2.0")
    os.environ.setdefault("PUSH_STREAM_RECONNECT_BACKOFF_MAX_SEC", "60.0")
    os.environ.setdefault("PUSH_STREAM_RECONNECT_STABLE_RESET_SEC", "45.0")
    os.environ.setdefault("PUSH_STREAM_SHORT_LIVED_SEC", "5.0")
    os.environ.setdefault("PUSH_STREAM_SHORT_LIVED_EXTRA_COOLDOWN_SEC", "20.0")

    # 既定は非破壊refresh。必要時だけ環境変数で戻せる。
    os.environ.setdefault("PUSH_STREAM_ONOPEN_REFRESH_FORCE", "0")
    os.environ.setdefault("PUSH_STREAM_ONOPEN_REFRESH_CLEAR_FIRST", "0")
    os.environ.setdefault("PUSH_STREAM_ONOPEN_REFRESH_UNREGISTER_FIRST", "0")
    os.environ.setdefault("PUSH_STREAM_ONOPEN_REFRESH_WAIT_AFTER_CLEAR", "0.0")

    # kabu Station WebSocketは単一オーナーに寄せる。
    os.environ.setdefault("PUSH_STREAM_SINGLE_OWNER_LOCK", "1")


def _lock_path() -> str:
    return os.getenv("PUSH_STREAM_SINGLE_OWNER_LOCK_PATH") or os.path.join(
        tempfile.gettempdir(),
        "autostock_kabustation_push_ws.lock",
    )


def _try_acquire_single_owner_lock() -> tuple[bool, Any]:
    if not _env_bool("PUSH_STREAM_SINGLE_OWNER_LOCK", True):
        return True, None
    path = _lock_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    except Exception:
        pass

    try:
        fh = open(path, "a+", encoding="utf-8")
    except Exception:
        logger.exception("[PUSH RECONNECT STABILITY] single-owner lock open failed path=%s -> allow", path)
        return True, None

    try:
        if os.name == "nt":
            import msvcrt
            fh.seek(0)
            try:
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                logger.warning(
                    "[PUSH RECONNECT STABILITY] another process owns PUSH WebSocket lock path=%s -> skip this runner",
                    path,
                )
                try:
                    fh.close()
                except Exception:
                    pass
                return False, None
        else:
            import fcntl
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                logger.warning(
                    "[PUSH RECONNECT STABILITY] another process owns PUSH WebSocket lock path=%s -> skip this runner",
                    path,
                )
                try:
                    fh.close()
                except Exception:
                    pass
                return False, None

        fh.seek(0)
        fh.truncate()
        fh.write(f"pid={os.getpid()} started_at={time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        fh.flush()
        logger.warning("[PUSH RECONNECT STABILITY] acquired PUSH WebSocket single-owner lock path=%s pid=%s", path, os.getpid())
        return True, fh
    except Exception:
        logger.exception("[PUSH RECONNECT STABILITY] single-owner lock failed path=%s -> allow", path)
        try:
            fh.close()
        except Exception:
            pass
        return True, None


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
        if getattr(cur, "_push_reconnect_stability_v2", False):
            return True

        def _safe_refresh_subscriptions_after_open_patched() -> None:
            try:
                if transport._env_bool("PUSH_STREAM_SKIP_AFTER_OPEN_REFRESH", True):
                    logger.warning("[push_stream] refresh after open skipped by env/default PUSH_STREAM_SKIP_AFTER_OPEN_REFRESH=1")
                    return

                delay = max(
                    float(getattr(transport, "AFTER_OPEN_REFRESH_DELAY_SEC", 0.0)),
                    _env_float("PUSH_STREAM_AFTER_OPEN_REFRESH_DELAY_SEC", 8.0),
                )
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
                transport._call_refresh(
                    force=force,
                    reason="on_open",
                    clear_first=clear_first,
                    unregister_first=unregister_first,
                    wait_after_clear=wait_after_clear,
                )
            except Exception:
                logger.exception("[push_stream] refresh after open failed")

        _safe_refresh_subscriptions_after_open_patched._push_reconnect_stability_v1 = True  # type: ignore[attr-defined]
        _safe_refresh_subscriptions_after_open_patched._push_reconnect_stability_v2 = True  # type: ignore[attr-defined]
        _safe_refresh_subscriptions_after_open_patched._original = cur  # type: ignore[attr-defined]
        transport._safe_refresh_subscriptions_after_open = _safe_refresh_subscriptions_after_open_patched
        logger.warning("[PUSH RECONNECT STABILITY] patched transport on_open refresh skipped_by_default=True")
        return True
    except Exception:
        logger.exception("[PUSH RECONNECT STABILITY] transport patch failed")
        return False


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
        if getattr(cur, "_push_reconnect_stability_v2", False):
            return True
        original = cur

        def _run_forever_loop_patched() -> None:
            global _LOCK_HANDLE
            ok_lock, lock_handle = _try_acquire_single_owner_lock()
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
        _run_forever_loop_patched._original = original  # type: ignore[attr-defined]
        runner._run_forever_loop = _run_forever_loop_patched
        logger.warning("[PUSH RECONNECT STABILITY] patched runner reconnect cooldown=True single_owner=True")
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
        logger.warning("[PUSH RECONNECT STABILITY] installed v2")
        return True
    if retry and not _INSTALLING:
        _INSTALLING = True

        def _loop() -> None:
            global _INSTALLED, _INSTALLING
            try:
                for _ in range(120):
                    if _apply():
                        _INSTALLED = True
                        logger.warning("[PUSH RECONNECT STABILITY] installed v2 by retry")
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
