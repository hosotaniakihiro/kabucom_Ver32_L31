# -*- coding: utf-8 -*-
# ============================================================
# File   : core/startup/summary_main_1m_light_tick_patch.py
# Version: V1-MAIN-1M-SUMMARY-LIGHT-TICK-ASYNC-AI
# ------------------------------------------------------------
# Purpose:
#   Keep main.py summary_parent_tick responsive.
#
#   runner_core.job_summary(PUSH) currently performs:
#     runner/fallback -> normalize -> save/spool -> AI entry hook -> display submit
#
#   The AI hook can take many seconds, making the parent tick approach/trigger
#   the 18s watchdog even when wait_push_targets=[1].  In main.py, order/entry
#   logic may run asynchronously; the scheduler tick itself should not wait.
#
#   This patch, only for main.py / entry-only process:
#     - skips synchronous main-side summary save/spool by default
#     - runs SUMMARY AI entry hook asynchronously for PUSH 1m/3m/5m
#
#   main_database.py remains the owner of DB persistence.
# ============================================================
from __future__ import annotations

import datetime as dt
import logging
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)
VERSION = "V1-MAIN-1M-SUMMARY-LIGHT-TICK-ASYNC-AI"
_INSTALLED = False
_AI_EXECUTOR: ThreadPoolExecutor | None = None
_AI_LOCK = threading.RLock()
_AI_RUNNING_KEYS: set[str] = set()


def _env_bool(name: str, default: bool = False) -> bool:
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


def _is_main_py() -> bool:
    try:
        argv = " ".join(str(x).replace("\\", "/").lower() for x in sys.argv)
        if any(x in argv for x in ("main_database.py", "data_collectors_runner.py", "summary_database_runner.py", "push_receiver_runner.py")):
            return False
        return "main.py" in argv
    except Exception:
        return False


def _is_entry_only_context() -> bool:
    try:
        role = str(os.getenv("SUMMARY_DB_WRITER_ROLE") or "").strip().lower()
        return _is_main_py() or _env_bool("SUMMARY_MAIN_ENTRY_ONLY", False) or role in {"entry_only", "main_entry_only", "read_only", "no_save"}
    except Exception:
        return _is_main_py()


def _executor() -> ThreadPoolExecutor:
    global _AI_EXECUTOR
    if _AI_EXECUTOR is None:
        workers = 1
        try:
            workers = max(1, int(float(os.getenv("SUMMARY_MAIN_ASYNC_AI_WORKERS", "1"))))
        except Exception:
            workers = 1
        _AI_EXECUTOR = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="summary-main-ai-async")
    return _AI_EXECUTOR


def _dt_key(now: Any) -> str:
    try:
        if isinstance(now, dt.datetime):
            return now.strftime("%Y%m%d%H%M%S")
        return str(now)
    except Exception:
        return "unknown"


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    if not _is_entry_only_context():
        logger.warning("[SUMMARY MAIN LIGHT TICK] skipped non-main context version=%s", VERSION)
        return False

    try:
        os.environ.setdefault("SUMMARY_MAIN_ASYNC_AI_ENTRY", "1")
        os.environ.setdefault("SUMMARY_MAIN_SKIP_SYNC_SAVE", "1")
        os.environ.setdefault("SUMMARY_MAIN_ASYNC_AI_WORKERS", "1")

        import scheduler_jobs.summary.runner_core as rc

        orig_ai = getattr(rc, "_run_push_ai_entry_before_display", None)
        orig_save = getattr(rc, "_save_summary_if_owner", None)

        if callable(orig_save) and not getattr(orig_save, "_main_light_tick_wrapped", False):
            def _save_summary_if_owner_light(df: pd.DataFrame, interval: int, *, source: str) -> None:
                if _is_entry_only_context() and _env_bool("SUMMARY_MAIN_SKIP_SYNC_SAVE", True):
                    logger.warning(
                        "[SUMMARY MAIN LIGHT TICK] sync save skipped in main interval=%s source=%s rows=%s reason=database_owner_main_database",
                        interval,
                        source,
                        len(df) if isinstance(df, pd.DataFrame) else 0,
                    )
                    return None
                return orig_save(df, interval, source=source)

            _save_summary_if_owner_light._main_light_tick_wrapped = True  # type: ignore[attr-defined]
            _save_summary_if_owner_light._original = orig_save  # type: ignore[attr-defined]
            rc._save_summary_if_owner = _save_summary_if_owner_light

        if callable(orig_ai) and not getattr(orig_ai, "_main_light_tick_wrapped", False):
            def _run_push_ai_entry_before_display_light(df: pd.DataFrame, interval: int, now: dt.datetime, run_entry: bool) -> None:
                if not (_is_entry_only_context() and _env_bool("SUMMARY_MAIN_ASYNC_AI_ENTRY", True)):
                    return orig_ai(df, interval, now, run_entry)
                if not (run_entry and int(interval) in (1, 3, 5)):
                    logger.info(
                        "[SUMMARY MAIN LIGHT TICK] async AI skipped interval=%s run_entry=%s reason=%s",
                        interval,
                        run_entry,
                        "interval_not_enabled" if int(interval) not in (1, 3, 5) else "run_entry_false",
                    )
                    return None

                rows = len(df) if isinstance(df, pd.DataFrame) else 0
                key = f"summary-ai:{int(interval)}:{_dt_key(now)}"
                with _AI_LOCK:
                    if key in _AI_RUNNING_KEYS:
                        logger.warning("[SUMMARY MAIN LIGHT TICK] async AI skipped already_running key=%s rows=%s", key, rows)
                        return None
                    _AI_RUNNING_KEYS.add(key)

                df_copy = df.copy(deep=False) if isinstance(df, pd.DataFrame) else df

                def _task() -> None:
                    try:
                        logger.warning("[SUMMARY MAIN LIGHT TICK] async AI start key=%s interval=%s rows=%s now=%s", key, interval, rows, now)
                        try:
                            from scheduler_jobs.summary.summary_ai_entry_hook_v20 import run_summary_ai_entry_safe
                            run_summary_ai_entry_safe(interval=int(interval), now=now, df=df_copy, source="SUMMARY")
                        except Exception:
                            logger.exception("[SUMMARY MAIN LIGHT TICK] async AI failed key=%s interval=%s", key, interval)
                    finally:
                        with _AI_LOCK:
                            _AI_RUNNING_KEYS.discard(key)
                        logger.warning("[SUMMARY MAIN LIGHT TICK] async AI done key=%s interval=%s", key, interval)

                _executor().submit(_task)
                logger.warning("[SUMMARY MAIN LIGHT TICK] async AI submitted key=%s interval=%s rows=%s now=%s", key, interval, rows, now)
                return None

            _run_push_ai_entry_before_display_light._main_light_tick_wrapped = True  # type: ignore[attr-defined]
            _run_push_ai_entry_before_display_light._original = orig_ai  # type: ignore[attr-defined]
            rc._run_push_ai_entry_before_display = _run_push_ai_entry_before_display_light

        _INSTALLED = True
        logger.warning(
            "[SUMMARY MAIN LIGHT TICK] installed version=%s async_ai=%s skip_sync_save=%s main=%s",
            VERSION,
            os.getenv("SUMMARY_MAIN_ASYNC_AI_ENTRY"),
            os.getenv("SUMMARY_MAIN_SKIP_SYNC_SAVE"),
            _is_main_py(),
        )
        return True
    except Exception:
        logger.exception("[SUMMARY MAIN LIGHT TICK] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[SUMMARY MAIN LIGHT TICK] auto install failed")

__all__ = ["VERSION", "install"]
