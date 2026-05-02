# ============================================================
# File   : core/startup/summary_runtime_pkg/state.py
# Version: REV3.0-SUMMARY-RUNTIME-STATE
# ------------------------------------------------------------
# 【概要】
#   summary bootstrap / post hook / DB seed の状態管理
#
# 【主な機能】
#   - bootstrap flags
#   - thread handle
#   - post-bootstrap hook flags
#   - DB seed flags
#   - global_data への状態反映
# ============================================================

from __future__ import annotations

import logging
import threading

from global_state import global_data

logger = logging.getLogger(__name__)

SUMMARY_TFS = (1, 3, 5)

SUMMARY_DB_SEED_BARS_PER_SYMBOL = {
    1: 180,
    3: 150,
    5: 150,
}

SUMMARY_DB_SEED_MIN_ROWS = {
    1: 1,
    3: 1,
    5: 1,
}

SUMMARY_TABLE_BY_TF = {
    1: "stock_summary_1min",
    3: "stock_summary_3min",
    5: "stock_summary_5min",
}

DEFAULT_SUMMARY_DIR = r"\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\summary"

SUMMARY_BOOTSTRAP_THREAD: threading.Thread | None = None

SUMMARY_BOOTSTRAP_STARTED = False
SUMMARY_BOOTSTRAP_DONE = False
SUMMARY_BOOTSTRAP_FAILED = False

POST_BOOTSTRAP_HOOK_RUNNING = False
POST_BOOTSTRAP_HOOK_DONE = False
POST_BOOTSTRAP_HOOK_FAILED = False

RUNTIME_DB_SEED_RUNNING = False
RUNTIME_DB_SEED_DONE = False
RUNTIME_DB_SEED_FAILED = False


def set_summary_bootstrap_flags(*, started=None, done=None, failed=None):
    global SUMMARY_BOOTSTRAP_STARTED, SUMMARY_BOOTSTRAP_DONE, SUMMARY_BOOTSTRAP_FAILED

    if started is not None:
        SUMMARY_BOOTSTRAP_STARTED = bool(started)
        try:
            global_data.summary_bootstrap_started = bool(started)
        except Exception:
            pass

    if done is not None:
        SUMMARY_BOOTSTRAP_DONE = bool(done)
        try:
            global_data.summary_bootstrap_done = bool(done)
        except Exception:
            pass

    if failed is not None:
        SUMMARY_BOOTSTRAP_FAILED = bool(failed)
        try:
            global_data.summary_bootstrap_failed = bool(failed)
        except Exception:
            pass


def get_summary_bootstrap_state() -> dict[str, bool]:
    return {
        "started": bool(SUMMARY_BOOTSTRAP_STARTED),
        "done": bool(SUMMARY_BOOTSTRAP_DONE),
        "failed": bool(SUMMARY_BOOTSTRAP_FAILED),
    }


def is_summary_bootstrap_running() -> bool:
    return bool(
        SUMMARY_BOOTSTRAP_STARTED
        and not SUMMARY_BOOTSTRAP_DONE
        and not SUMMARY_BOOTSTRAP_FAILED
    )


def mark_bootstrap_thread_done_ok() -> None:
    set_summary_bootstrap_flags(done=True, failed=False)
    try:
        global_data.summary_bootstrap_running = False
    except Exception:
        pass


def mark_bootstrap_thread_failed() -> None:
    set_summary_bootstrap_flags(done=False, failed=True)
    try:
        global_data.summary_bootstrap_running = False
    except Exception:
        pass


def mark_bootstrap_thread_running() -> None:
    set_summary_bootstrap_flags(started=True, done=False, failed=False)
    try:
        global_data.summary_bootstrap_running = True
    except Exception:
        pass


def set_runtime_db_seed_flags(*, running=None, done=None, failed=None) -> None:
    global RUNTIME_DB_SEED_RUNNING, RUNTIME_DB_SEED_DONE, RUNTIME_DB_SEED_FAILED

    if running is not None:
        RUNTIME_DB_SEED_RUNNING = bool(running)
        try:
            global_data.summary_runtime_db_seed_running = bool(running)
        except Exception:
            pass

    if done is not None:
        RUNTIME_DB_SEED_DONE = bool(done)
        try:
            global_data.summary_runtime_db_seed_done = bool(done)
        except Exception:
            pass

    if failed is not None:
        RUNTIME_DB_SEED_FAILED = bool(failed)
        try:
            global_data.summary_runtime_db_seed_failed = bool(failed)
        except Exception:
            pass


def get_runtime_db_seed_state() -> dict[str, bool]:
    return {
        "running": bool(RUNTIME_DB_SEED_RUNNING),
        "done": bool(RUNTIME_DB_SEED_DONE),
        "failed": bool(RUNTIME_DB_SEED_FAILED),
    }


def set_post_hook_flags(*, running=None, done=None, failed=None) -> None:
    global POST_BOOTSTRAP_HOOK_RUNNING, POST_BOOTSTRAP_HOOK_DONE, POST_BOOTSTRAP_HOOK_FAILED

    if running is not None:
        POST_BOOTSTRAP_HOOK_RUNNING = bool(running)

    if done is not None:
        POST_BOOTSTRAP_HOOK_DONE = bool(done)

    if failed is not None:
        POST_BOOTSTRAP_HOOK_FAILED = bool(failed)


def get_post_hook_state() -> dict[str, bool]:
    return {
        "running": bool(POST_BOOTSTRAP_HOOK_RUNNING),
        "done": bool(POST_BOOTSTRAP_HOOK_DONE),
        "failed": bool(POST_BOOTSTRAP_HOOK_FAILED),
    }


def reset_post_hook_state() -> None:
    set_post_hook_flags(running=False, done=False, failed=False)


__all__ = [
    "SUMMARY_TFS",
    "SUMMARY_DB_SEED_BARS_PER_SYMBOL",
    "SUMMARY_DB_SEED_MIN_ROWS",
    "SUMMARY_TABLE_BY_TF",
    "DEFAULT_SUMMARY_DIR",
    "SUMMARY_BOOTSTRAP_THREAD",
    "SUMMARY_BOOTSTRAP_STARTED",
    "SUMMARY_BOOTSTRAP_DONE",
    "SUMMARY_BOOTSTRAP_FAILED",
    "POST_BOOTSTRAP_HOOK_RUNNING",
    "POST_BOOTSTRAP_HOOK_DONE",
    "POST_BOOTSTRAP_HOOK_FAILED",
    "RUNTIME_DB_SEED_RUNNING",
    "RUNTIME_DB_SEED_DONE",
    "RUNTIME_DB_SEED_FAILED",
    "set_summary_bootstrap_flags",
    "get_summary_bootstrap_state",
    "is_summary_bootstrap_running",
    "mark_bootstrap_thread_done_ok",
    "mark_bootstrap_thread_failed",
    "mark_bootstrap_thread_running",
    "set_runtime_db_seed_flags",
    "get_runtime_db_seed_state",
    "set_post_hook_flags",
    "get_post_hook_state",
    "reset_post_hook_state",
]