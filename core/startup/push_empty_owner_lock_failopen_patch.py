# ============================================================
# File   : core/startup/push_empty_owner_lock_failopen_patch.py
# Version: V1.0-PUSH-EMPTY-OWNER-LOCK-FAILOPEN
# ------------------------------------------------------------
# 目的:
#   PUSH single-owner lock が OS 的には掴まれているのに、lock file の
#   owner text が空で owner_pid も読めない場合、main.py が永久に
#   connected=False / total_received=0 で待ち続けるのを防ぐ。
#
# 方針:
#   main.py だけ、空owner lock で一定秒数待った後は lock_handle=None の
#   fail-open で WebSocket 起動へ進める。
#   DB/collector/push_receiver 系は対象外。
# ============================================================

from __future__ import annotations

import logging
import os
import sys
import time
from typing import Any

logger = logging.getLogger(__name__)
_INSTALLED = False


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).replace(",", ""))
    except Exception:
        return float(default)


def _env_bool(name: str, default: bool) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _is_main_py_context() -> bool:
    try:
        argv = " ".join(str(x).replace("\\", "/").lower() for x in sys.argv)
        return "main.py" in argv and "main_database.py" not in argv
    except Exception:
        return False


def _is_excluded_context() -> bool:
    try:
        argv = " ".join(str(x).replace("\\", "/").lower() for x in sys.argv)
        if any(x in argv for x in (
            "main_database.py",
            "db_prepare_runner.py",
            "ranking_collector_runner.py",
            "push_receiver_runner.py",
            "yahoo_complement_runner.py",
            "summary_database_runner.py",
            "data_collectors_runner.py",
        )):
            return True
        for key in (
            "AUTOSTOCK_DATA_COLLECTORS_PROCESS",
            "AUTOSTOCK_MAIN_DATABASE_PROCESS",
            "AUTOSTOCK_SUMMARY_DB_WRITER",
            "AUTOSTOCK_RANKING_COLLECTOR_PROCESS",
        ):
            if os.getenv(key) == "1":
                return True
    except Exception:
        pass
    return False


def _is_empty_unknown_owner_detail(detail: Any) -> bool:
    if not isinstance(detail, dict):
        return False
    text = str(detail.get("text") or "").strip()
    owner_pid = detail.get("owner_pid")
    owner_alive = detail.get("owner_alive")
    return text == "" and owner_pid in (None, "", 0) and owner_alive in (None, "")


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True

    os.environ.setdefault("PUSH_STREAM_EMPTY_OWNER_FAILOPEN", "1")
    os.environ.setdefault("PUSH_STREAM_EMPTY_OWNER_FAILOPEN_SEC", "20.0")

    if not _env_bool("PUSH_STREAM_EMPTY_OWNER_FAILOPEN", True):
        logger.warning("[PUSH EMPTY OWNER FAILOPEN] disabled by env")
        return False
    if _is_excluded_context() or not _is_main_py_context():
        logger.warning(
            "[PUSH EMPTY OWNER FAILOPEN] skipped context main_py=%s excluded=%s argv=%s",
            _is_main_py_context(),
            _is_excluded_context(),
            sys.argv,
        )
        return False

    try:
        from core.startup import push_stream_reconnect_stability_patch as p
    except Exception:
        logger.exception("[PUSH EMPTY OWNER FAILOPEN] stability patch import failed")
        return False

    old_wait = getattr(p, "_wait_for_single_owner_lock", None)
    if not callable(old_wait):
        logger.warning("[PUSH EMPTY OWNER FAILOPEN] _wait_for_single_owner_lock unavailable")
        return False
    if getattr(old_wait, "_push_empty_owner_failopen_v1", False):
        _INSTALLED = True
        return True

    old_try = getattr(p, "_try_acquire_single_owner_lock", None)
    old_env_float = getattr(p, "_env_float", _env_float)

    def _wait_for_single_owner_lock_failopen(state: Any, _safe_set_runtime: Any) -> Any:
        retry = max(1.0, old_env_float("PUSH_STREAM_SINGLE_OWNER_RETRY_SEC", 5.0))
        log_every = max(retry, old_env_float("PUSH_STREAM_SINGLE_OWNER_LOG_EVERY_SEC", 30.0))
        failopen_sec = max(5.0, _env_float("PUSH_STREAM_EMPTY_OWNER_FAILOPEN_SEC", 20.0))
        next_log = 0.0
        empty_owner_started: float | None = None

        while not state._stop_event.is_set():
            ok_lock, lock_handle, detail = old_try()
            if ok_lock:
                _safe_set_runtime("push_stream_skipped_reason", "")
                return lock_handle

            now = time.monotonic()
            if _is_empty_unknown_owner_detail(detail):
                if empty_owner_started is None:
                    empty_owner_started = now
                elapsed_empty = now - empty_owner_started
                if elapsed_empty >= failopen_sec:
                    _safe_set_runtime("push_stream_skipped_reason", "empty_owner_lock_failopen")
                    logger.warning(
                        "[PUSH EMPTY OWNER FAILOPEN] fail-open after %.1fs detail=%s argv=%s",
                        elapsed_empty,
                        detail,
                        sys.argv,
                    )
                    return None
            else:
                empty_owner_started = None

            _safe_set_runtime("push_stream_running", False)
            _safe_set_runtime("push_stream_skipped_reason", "single_owner_lock_held_waiting")
            if now >= next_log:
                logger.warning(
                    "[push_stream] waiting for PUSH WebSocket single-owner lock retry_sec=%.1f failopen_sec=%.1f empty_wait=%s detail=%s",
                    retry,
                    failopen_sec,
                    None if empty_owner_started is None else round(now - empty_owner_started, 1),
                    detail,
                )
                next_log = now + log_every
            time.sleep(retry)
        return None

    _wait_for_single_owner_lock_failopen._push_empty_owner_failopen_v1 = True  # type: ignore[attr-defined]
    _wait_for_single_owner_lock_failopen._original = old_wait  # type: ignore[attr-defined]
    p._wait_for_single_owner_lock = _wait_for_single_owner_lock_failopen

    _INSTALLED = True
    logger.warning(
        "[PUSH EMPTY OWNER FAILOPEN] installed failopen_sec=%s argv=%s",
        os.getenv("PUSH_STREAM_EMPTY_OWNER_FAILOPEN_SEC"),
        sys.argv,
    )
    return True


try:
    install()
except Exception:
    logger.exception("[PUSH EMPTY OWNER FAILOPEN] auto install failed")


__all__ = ["install"]
