# -*- coding: utf-8 -*-
"""
Force database writes to the main_database.py / data collector process.

Purpose
-------
main.py should remain an entry/display/realtime process.  It may calculate
summary data for entry decisions, but it must not persist PUSH/Yahoo/summary
rows when main_database.py is used as the DB owner.

This patch is intentionally defensive:
- sets owner environment variables early
- patches data_collectors.split_mode helper decisions
- patches scheduler_jobs.summary.runner_core save gate when already imported
- keeps data collector / main_database process writable
"""
from __future__ import annotations

import logging
import os
import sys
from typing import Any

logger = logging.getLogger(__name__)
VERSION = "V1-DATABASE-OWNER-ONLY"
_INSTALLED = False

_TRUE_VALUES = {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
_FALSE_VALUES = {"0", "false", "no", "n", "off", "disable", "disabled"}


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None:
        return bool(default)
    s = str(v).strip().lower()
    if s in _TRUE_VALUES:
        return True
    if s in _FALSE_VALUES:
        return False
    return bool(default)


def _argv_context() -> str:
    try:
        text = " ".join(str(x).replace("\\", "/").lower() for x in sys.argv)
        if "main_database.py" in text or "data_collectors_runner" in text:
            return "main_database"
        if "main.py" in text:
            return "main"
        if text:
            return "helper"
    except Exception:
        pass
    return "unknown"


def _is_database_process() -> bool:
    context = _argv_context()
    return (
        context == "main_database"
        or _env_bool("AUTOSTOCK_DATA_COLLECTORS_PROCESS", False)
        or _env_bool("AUTOSTOCK_MAIN_DATABASE_PROCESS", False)
        or _env_bool("AUTOSTOCK_SUMMARY_DB_WRITER", False)
    )


def _is_main_process() -> bool:
    return _argv_context() == "main" and not _is_database_process()


def _apply_env_defaults() -> None:
    # DB owner is always main_database/data_collector unless explicitly disabled.
    os.environ.setdefault("AUTOSTOCK_EXTERNAL_DATA_COLLECTORS", "1")
    os.environ.setdefault("AUTOSTOCK_SUMMARY_SAVE_OWNER", "database")
    os.environ.setdefault("AUTOSTOCK_YAHOO_COMPLEMENT_OWNER", "database")

    if _is_database_process():
        os.environ.setdefault("AUTOSTOCK_DATA_COLLECTORS_PROCESS", "1")
        os.environ.setdefault("SUMMARY_DB_WRITER_ROLE", "database")
        os.environ.setdefault("SUMMARY_SKIP_DB_SAVE_IN_MAIN", "0")
        os.environ.setdefault("AUTOSTOCK_MAIN_MEMORY_ONLY", "0")
        os.environ.setdefault("AUTOSTOCK_SKIP_DATA_COLLECTOR_WORK_IN_MAIN", "0")
    elif _is_main_process():
        # main.py is explicitly memory/entry only.  Do not use setdefault here;
        # this must override stale shell variables from older runs.
        os.environ["AUTOSTOCK_MAIN_MEMORY_ONLY"] = "1"
        os.environ["AUTOSTOCK_SKIP_DATA_COLLECTOR_WORK_IN_MAIN"] = "1"
        os.environ["SUMMARY_MAIN_ENTRY_ONLY"] = "1"
        os.environ["SUMMARY_SKIP_DB_SAVE_IN_MAIN"] = "1"
        os.environ["SUMMARY_DB_WRITER_ROLE"] = "entry_only"
        os.environ["AUTOSTOCK_SUMMARY_SAVE_OWNER"] = "database"
        os.environ["AUTOSTOCK_YAHOO_COMPLEMENT_OWNER"] = "database"
        os.environ.setdefault("AUTOSTOCK_ENTRY_ONLY_PROCESS", "1")


def _patch_split_mode() -> bool:
    try:
        import data_collectors.split_mode as sm

        def is_data_collector_process() -> bool:
            return _is_database_process()

        def external_data_collectors_enabled() -> bool:
            return True

        def main_memory_only_enabled() -> bool:
            return _is_main_process() or _env_bool("AUTOSTOCK_MAIN_MEMORY_ONLY", False)

        def yahoo_complement_owner() -> str:
            return "database"

        def summary_save_owner() -> str:
            return "database"

        def should_skip_data_collector_work_in_main() -> bool:
            return _is_main_process()

        def should_run_yahoo_complement_in_this_process() -> bool:
            return _is_database_process()

        def should_run_summary_save_in_this_process() -> bool:
            return _is_database_process()

        def should_skip_yahoo_complement_in_main() -> bool:
            return not _is_database_process()

        def should_skip_summary_save_in_this_process() -> bool:
            return not _is_database_process()

        def mark_as_data_collector_process() -> None:
            os.environ["AUTOSTOCK_DATA_COLLECTORS_PROCESS"] = "1"
            os.environ["AUTOSTOCK_EXTERNAL_DATA_COLLECTORS"] = "1"
            os.environ["AUTOSTOCK_YAHOO_COMPLEMENT_OWNER"] = "database"
            os.environ["AUTOSTOCK_SUMMARY_SAVE_OWNER"] = "database"
            os.environ["SUMMARY_DB_WRITER_ROLE"] = "database"

        sm.is_data_collector_process = is_data_collector_process
        sm.external_data_collectors_enabled = external_data_collectors_enabled
        sm.main_memory_only_enabled = main_memory_only_enabled
        sm.yahoo_complement_owner = yahoo_complement_owner
        sm.summary_save_owner = summary_save_owner
        sm.should_skip_data_collector_work_in_main = should_skip_data_collector_work_in_main
        sm.should_run_yahoo_complement_in_this_process = should_run_yahoo_complement_in_this_process
        sm.should_run_summary_save_in_this_process = should_run_summary_save_in_this_process
        sm.should_skip_yahoo_complement_in_main = should_skip_yahoo_complement_in_main
        sm.should_skip_summary_save_in_this_process = should_skip_summary_save_in_this_process
        sm.mark_as_data_collector_process = mark_as_data_collector_process
        logger.warning("[DATABASE OWNER] split_mode patched db_process=%s main=%s", _is_database_process(), _is_main_process())
        return True
    except Exception:
        logger.exception("[DATABASE OWNER] split_mode patch failed")
        return False


def _patch_summary_runner_core() -> bool:
    try:
        import scheduler_jobs.summary.runner_core as rc

        def _summary_save_enabled() -> bool:
            mode = str(os.environ.get("AUTOSTOCK_SUMMARY_SAVE_MODE", "")).strip().lower()
            if mode in {"disabled", "disable", "calculate_only", "calc_only", "no_save", "skip", "off"}:
                return False
            # Even if a stale env says enabled, main.py must remain non-writer.
            if _is_main_process() or os.environ.get("SUMMARY_SKIP_DB_SAVE_IN_MAIN", "").strip().lower() in _TRUE_VALUES:
                return False
            return _is_database_process()

        def _is_database_process_gate() -> bool:
            return _is_database_process()

        rc._summary_save_enabled = _summary_save_enabled
        rc._is_database_process = _is_database_process_gate
        logger.warning("[DATABASE OWNER] summary runner save gate patched db_process=%s main=%s", _is_database_process(), _is_main_process())
        return True
    except Exception:
        # runner_core may not be imported in some helper processes; this is not fatal.
        logger.debug("[DATABASE OWNER] summary runner gate patch skipped", exc_info=True)
        return False


def _patch_push_storage_bootstrap() -> bool:
    try:
        import core.startup.push_storage_bootstrap as psb

        def _should_skip_push_storage_start_in_main() -> bool:
            return _is_main_process()

        psb._should_skip_push_storage_start_in_main = _should_skip_push_storage_start_in_main
        logger.warning("[DATABASE OWNER] push storage bootstrap gate patched db_process=%s main=%s", _is_database_process(), _is_main_process())
        return True
    except Exception:
        logger.debug("[DATABASE OWNER] push storage bootstrap patch skipped", exc_info=True)
        return False


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    try:
        _apply_env_defaults()
        split_ok = _patch_split_mode()
        summary_ok = _patch_summary_runner_core()
        push_ok = _patch_push_storage_bootstrap()
        _INSTALLED = True
        logger.warning(
            "[DATABASE OWNER] installed version=%s context=%s db_process=%s main_process=%s split=%s summary_gate=%s push_storage=%s summary_owner=%s yahoo_owner=%s main_memory_only=%s",
            VERSION,
            _argv_context(),
            _is_database_process(),
            _is_main_process(),
            split_ok,
            summary_ok,
            push_ok,
            os.environ.get("AUTOSTOCK_SUMMARY_SAVE_OWNER"),
            os.environ.get("AUTOSTOCK_YAHOO_COMPLEMENT_OWNER"),
            os.environ.get("AUTOSTOCK_MAIN_MEMORY_ONLY"),
        )
        return True
    except Exception:
        logger.exception("[DATABASE OWNER] install failed")
        return False


__all__ = ["VERSION", "install"]
