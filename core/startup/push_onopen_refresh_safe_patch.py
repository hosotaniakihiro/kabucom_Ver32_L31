# ============================================================
# File   : core/startup/push_onopen_refresh_safe_patch.py
# Version: V2.1-PUSH-ONOPEN-STABLE-DELAYED-REFRESH
# ------------------------------------------------------------
# 目的:
#   WebSocket reconnect直後の非破壊refreshで kabu Station を再切断させない。
#
# 背景:
#   V2.0 は delay=0.05s / ready_wait=0.75s で on_open_safe_fast を実行していた。
#   ログ上では CONNECTED 直後に refresh が入り、その直後に connected=False へ戻る
#   ケースがあるため、rotation_core の ws stable grace と同じ考え方に寄せる。
#
# 方針:
#   - on_open直後は最低3秒待つ。
#   - ws ready確認も4秒まで待つ。
#   - min_intervalは15秒へ延長する。
#   - clear/unregisterは常にFalse既定のまま。
#   - refreshは非破壊で1回だけ。通常のA/B登録はrotation workerへ任せる。
# ============================================================
from __future__ import annotations

import logging
import os
import threading
import time

logger = logging.getLogger(__name__)
_INSTALLED = False
_LAST_STARTED_TS = 0.0
_LOCK = threading.Lock()


def _env_bool(name: str, default: bool) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        s = str(v).strip().lower()
        if s in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}:
            return True
        if s in {"0", "false", "no", "n", "off", "ng", "disable", "disabled"}:
            return False
        return bool(default)
    except Exception:
        return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _force_env(name: str, value: str) -> None:
    try:
        old = os.environ.get(name)
        os.environ[name] = str(value)
        if old != str(value):
            logger.warning("[PUSH ONOPEN SAFE REFRESH] env force %s %s->%s", name, old, value)
    except Exception:
        pass


def _apply_defaults() -> None:
    _force_env("PUSH_STREAM_SKIP_AFTER_OPEN_REFRESH", "0")
    os.environ.setdefault("PUSH_STREAM_ONOPEN_SAFE_REFRESH_DELAY_SEC", "3.0")
    os.environ.setdefault("PUSH_STREAM_ONOPEN_SAFE_REFRESH_MIN_INTERVAL_SEC", "15.0")
    os.environ.setdefault("PUSH_STREAM_ONOPEN_SAFE_READY_WAIT_SEC", "4.0")
    os.environ.setdefault("PUSH_STREAM_ONOPEN_SAFE_FORCE", "0")
    os.environ.setdefault("PUSH_STREAM_ONOPEN_SAFE_CLEAR_FIRST", "0")
    os.environ.setdefault("PUSH_STREAM_ONOPEN_SAFE_UNREGISTER_FIRST", "0")
    os.environ.setdefault("PUSH_STREAM_ONOPEN_SAFE_WAIT_AFTER_CLEAR_SEC", "0.0")


def _ws_connected_and_alive() -> bool:
    try:
        from trading.push.push_stream import state
        from trading.push.push_stream.transport import _is_ws_alive
        return bool(state._connected_event.is_set()) and bool(_is_ws_alive())
    except Exception:
        return False


def _safe_onopen_refresh_worker() -> None:
    try:
        _apply_defaults()
        if not _env_bool("PUSH_STREAM_ONOPEN_SAFE_REFRESH_ENABLED", True):
            logger.warning("[PUSH ONOPEN SAFE REFRESH] skipped by env")
            return

        delay = max(0.0, _env_float("PUSH_STREAM_ONOPEN_SAFE_REFRESH_DELAY_SEC", 3.0))
        min_interval = max(1.0, _env_float("PUSH_STREAM_ONOPEN_SAFE_REFRESH_MIN_INTERVAL_SEC", 15.0))
        now = time.monotonic()
        global _LAST_STARTED_TS
        with _LOCK:
            since = now - float(_LAST_STARTED_TS or 0.0)
            if _LAST_STARTED_TS and since < min_interval:
                logger.warning(
                    "[PUSH ONOPEN SAFE REFRESH] skipped recent since=%.1fs min_interval=%.1fs",
                    since,
                    min_interval,
                )
                return
            _LAST_STARTED_TS = now

        logger.warning(
            "[PUSH ONOPEN SAFE REFRESH] stable delayed non-destructive refresh scheduled delay=%.2fs force=False clear_first=False unregister_first=False",
            delay,
        )
        if delay > 0:
            time.sleep(delay)

        if not _ws_connected_and_alive():
            logger.warning("[PUSH ONOPEN SAFE REFRESH] skipped: ws lost before delayed refresh")
            return

        try:
            from trading.push.push_stream import transport
        except Exception:
            logger.exception("[PUSH ONOPEN SAFE REFRESH] transport import failed")
            return

        wait_fn = getattr(transport, "_wait_for_ws_ready", None)
        call_fn = getattr(transport, "_call_refresh", None)
        if callable(wait_fn):
            if not bool(wait_fn(timeout=_env_float("PUSH_STREAM_ONOPEN_SAFE_READY_WAIT_SEC", 4.0))):
                logger.warning("[PUSH ONOPEN SAFE REFRESH] skipped: ws not ready after stable delay")
                return
        if not _ws_connected_and_alive():
            logger.warning("[PUSH ONOPEN SAFE REFRESH] skipped: ws lost after ready wait")
            return
        if not callable(call_fn):
            logger.warning("[PUSH ONOPEN SAFE REFRESH] skipped: _call_refresh not callable")
            return

        result = call_fn(
            force=False,
            reason="on_open_safe_stable",
            clear_first=False,
            unregister_first=False,
            wait_after_clear=0.0,
        )
        logger.warning("[PUSH ONOPEN SAFE REFRESH] done result_type=%s result=%r", type(result).__name__, result)
    except Exception:
        logger.exception("[PUSH ONOPEN SAFE REFRESH] worker failed")


def _patched_start_refresh_after_open_thread() -> None:
    try:
        _apply_defaults()
        threading.Thread(
            target=_safe_onopen_refresh_worker,
            name="push-onopen-safe-refresh-stable",
            daemon=True,
        ).start()
        logger.warning("[PUSH ONOPEN SAFE REFRESH] stable thread started")
    except Exception:
        logger.exception("[PUSH ONOPEN SAFE REFRESH] thread start failed")


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    try:
        _apply_defaults()
        from trading.push.push_stream import transport
        transport._start_refresh_after_open_thread = _patched_start_refresh_after_open_thread
        try:
            from trading.push.push_stream import ws_callbacks
            ws_callbacks._start_refresh_after_open_thread = _patched_start_refresh_after_open_thread
        except Exception:
            logger.debug("[PUSH ONOPEN SAFE REFRESH] ws_callbacks patch skipped", exc_info=True)

        _INSTALLED = True
        logger.warning("[PUSH ONOPEN SAFE REFRESH] installed v2.1 stable delayed")
        return True
    except Exception:
        logger.exception("[PUSH ONOPEN SAFE REFRESH] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[PUSH ONOPEN SAFE REFRESH] auto install failed")


__all__ = ["install"]
