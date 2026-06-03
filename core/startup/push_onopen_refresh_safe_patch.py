# ============================================================
# File   : core/startup/push_onopen_refresh_safe_patch.py
# Version: V1.0-PUSH-ONOPEN-REFRESH-SAFE-DELAY
# ------------------------------------------------------------
# 目的:
#   WebSocket reconnect 直後に clear/unregister/register を即実行すると、
#   kabu Station 側から WinError 10054 で切断されやすい。
#
#   on_open 後 refresh を、
#     - デフォルト12秒遅延
#     - force=False
#     - clear_first=False
#     - unregister_first=False
#   の安全再登録に差し替える。
#
#   rotation worker が別途動くため、on_open では「保険の軽い再登録」に留める。
# ============================================================
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

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


def _safe_onopen_refresh_worker() -> None:
    try:
        if not _env_bool("PUSH_STREAM_ONOPEN_SAFE_REFRESH_ENABLED", True):
            logger.warning("[PUSH ONOPEN SAFE REFRESH] skipped by env")
            return

        delay = max(0.0, _env_float("PUSH_STREAM_ONOPEN_SAFE_REFRESH_DELAY_SEC", 12.0))
        min_interval = max(1.0, _env_float("PUSH_STREAM_ONOPEN_SAFE_REFRESH_MIN_INTERVAL_SEC", 30.0))
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
            "[PUSH ONOPEN SAFE REFRESH] delayed safe refresh scheduled delay=%.1fs force=False clear_first=False unregister_first=False",
            delay,
        )
        time.sleep(delay)

        try:
            from trading.push.push_stream import transport
        except Exception:
            logger.exception("[PUSH ONOPEN SAFE REFRESH] transport import failed")
            return

        wait_fn = getattr(transport, "_wait_for_ws_ready", None)
        call_fn = getattr(transport, "_call_refresh", None)
        if callable(wait_fn):
            if not bool(wait_fn(timeout=_env_float("PUSH_STREAM_ONOPEN_SAFE_READY_WAIT_SEC", 3.0))):
                logger.warning("[PUSH ONOPEN SAFE REFRESH] skipped: ws not ready after delay")
                return

        if not callable(call_fn):
            logger.warning("[PUSH ONOPEN SAFE REFRESH] skipped: _call_refresh not callable")
            return

        result = call_fn(
            force=_env_bool("PUSH_STREAM_ONOPEN_SAFE_FORCE", False),
            reason="on_open_safe",
            clear_first=_env_bool("PUSH_STREAM_ONOPEN_SAFE_CLEAR_FIRST", False),
            unregister_first=_env_bool("PUSH_STREAM_ONOPEN_SAFE_UNREGISTER_FIRST", False),
            wait_after_clear=_env_float("PUSH_STREAM_ONOPEN_SAFE_WAIT_AFTER_CLEAR_SEC", 0.0),
        )
        logger.warning("[PUSH ONOPEN SAFE REFRESH] done result_type=%s result=%r", type(result).__name__, result)
    except Exception:
        logger.exception("[PUSH ONOPEN SAFE REFRESH] worker failed")


def _patched_start_refresh_after_open_thread() -> None:
    try:
        if _env_bool("PUSH_STREAM_SKIP_AFTER_OPEN_REFRESH", False):
            logger.warning("[PUSH ONOPEN SAFE REFRESH] thread not started by PUSH_STREAM_SKIP_AFTER_OPEN_REFRESH=1")
            return
        threading.Thread(
            target=_safe_onopen_refresh_worker,
            name="push-onopen-safe-refresh",
            daemon=True,
        ).start()
        logger.warning("[PUSH ONOPEN SAFE REFRESH] thread started")
    except Exception:
        logger.exception("[PUSH ONOPEN SAFE REFRESH] thread start failed")


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    try:
        from trading.push.push_stream import transport
        transport._start_refresh_after_open_thread = _patched_start_refresh_after_open_thread

        # ws_callbacks は `from .transport import _start_refresh_after_open_thread` で関数参照を持つため、
        # 既にimport済みの場合はそこも差し替える。
        try:
            from trading.push.push_stream import ws_callbacks
            ws_callbacks._start_refresh_after_open_thread = _patched_start_refresh_after_open_thread
        except Exception:
            logger.debug("[PUSH ONOPEN SAFE REFRESH] ws_callbacks patch skipped", exc_info=True)

        _INSTALLED = True
        logger.warning("[PUSH ONOPEN SAFE REFRESH] installed v1")
        return True
    except Exception:
        logger.exception("[PUSH ONOPEN SAFE REFRESH] install failed")
        return False
