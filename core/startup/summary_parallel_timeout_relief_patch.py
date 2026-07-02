# -*- coding: utf-8 -*-
# ============================================================
# File   : core/startup/summary_parallel_timeout_relief_patch.py
# Version: V2-MAIN-SUMMARY-SCHEDULER-1M-ONLY
# ------------------------------------------------------------
# Purpose:
#   Reduce main.py entry-tick latency and avoid repeated logs like:
#       [SUMMARY PARALLEL] tick timeout ...
#       [summary.scheduler] CALL timeout label=PUSH fn=<lambda> interval=1
#       [summary.scheduler] CALL timeout label=PUSH fn=job_5m interval=5
#
#   main.py should prioritize fresh 1m PUSH judgement.  Heavy/periodic 3m/5m
#   PUSH work is supplied by main_database.py, raw fallback/cache, and MTF
#   enrichment.  This patch therefore forces the main.py scheduler/fallback
#   path to run only the 1m PUSH job synchronously.
# ============================================================
from __future__ import annotations

import logging
import os
import sys
from typing import Any

logger = logging.getLogger(__name__)
VERSION = "V2-MAIN-SUMMARY-SCHEDULER-1M-ONLY"
_INSTALLED = False


def _is_main_py() -> bool:
    try:
        argv = " ".join(str(x).replace("\\", "/").lower() for x in sys.argv)
        return "main.py" in argv and "main_database.py" not in argv
    except Exception:
        return False


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _set_default(name: str, value: str) -> bool:
    try:
        old = os.getenv(name)
        if old is None or str(old).strip() == "":
            os.environ[name] = str(value)
            return True
        return False
    except Exception:
        return False


def _force(name: str, value: str) -> tuple[str | None, str]:
    old = os.getenv(name)
    os.environ[name] = str(value)
    return old, str(value)


def _patch_summary_parallel_module() -> bool:
    """Tune the optional parallel runtime patch when it is already imported."""
    try:
        import core.startup.summary_parallel_intervals_runtime_patch as target
    except Exception:
        return False

    patched = False
    for name, value in {
        "SUMMARY_PUSH_BG_ALL_INTERVALS": "0",
        "SUMMARY_PUSH_BG_LONG_INTERVALS": "0",
        "SUMMARY_PUSH_DISPLAY_ALL_INTERVALS": "0",
        "SUMMARY_PUSH_BG_INTERVAL_WORKERS": "1",
        "SUMMARY_PUSH_FORCE_1_3_5": "0",
        "SUMMARY_PARALLEL_FORCE_1_3_5": "0",
        "SUMMARY_PARALLEL_MAIN_ENTRY_ONLY": "1",
        "SUMMARY_PARALLEL_TIMEOUT_SEC": "20",
        "SUMMARY_PARALLEL_MIN_TIMEOUT_SEC": "20",
        "SUMMARY_PARALLEL_PARENT_TIMEOUT_SEC": "30",
        "SUMMARY_PARENT_TICK_TIMEOUT_SEC": "30",
        "SUMMARY_CHILD_JOB_TIMEOUT_SEC": "20",
        "SUMMARY_MAIN_PUSH_1M_ONLY": "1",
    }.items():
        _force(name, value)
        patched = True

    for attr in (
        "SUMMARY_PARALLEL_TIMEOUT_SEC",
        "SUMMARY_PARALLEL_MIN_TIMEOUT_SEC",
        "SUMMARY_CHILD_JOB_TIMEOUT_SEC",
        "SUMMARY_PARENT_TICK_TIMEOUT_SEC",
        "DEFAULT_TIMEOUT_SEC",
        "TIMEOUT_SEC",
        "MIN_TIMEOUT_SEC",
    ):
        try:
            if hasattr(target, attr):
                setattr(target, attr, 30.0 if "PARENT" in attr else 20.0)
                patched = True
        except Exception:
            pass
    return patched


def _patch_scheduler_module() -> bool:
    """Force scheduler_jobs.summary.scheduler to run only 1m PUSH in main.py.

    The scheduler itself normally decides 3m/5m by minute boundary.  In the
    entry-only main process, those long interval jobs can exceed the child
    timeout and keep fallback/parent threads busy.  Patching _run_flags keeps
    existing registration intact but makes parent/fallback ticks light.
    """
    if not _env_bool("SUMMARY_MAIN_PUSH_1M_ONLY", True):
        return False
    try:
        import scheduler_jobs.summary.scheduler as sch
    except Exception:
        return False

    try:
        cur = getattr(sch, "_run_flags", None)
        if getattr(cur, "_main_1m_only_v2", False):
            return True
    except Exception:
        pass

    def _run_flags_1m_only(now: Any = None):
        return True, False, False

    def _should_run_1m(now: Any = None) -> bool:
        return True

    def _should_run_false(now: Any = None) -> bool:
        return False

    try:
        _run_flags_1m_only._main_1m_only_v2 = True  # type: ignore[attr-defined]
        sch._run_flags = _run_flags_1m_only
        sch._should_run_1m = _should_run_1m
        sch._should_run_3m = _should_run_false
        sch._should_run_5m = _should_run_false
        sch.should_run_1m = _should_run_1m
        sch.should_run_3m = _should_run_false
        sch.should_run_5m = _should_run_false

        def _parent_timeout_sec() -> float:
            try:
                return float(os.getenv("SUMMARY_PARENT_TICK_TIMEOUT_SEC", "30") or 30.0)
            except Exception:
                return 30.0

        def _child_timeout_sec() -> float:
            try:
                return float(os.getenv("SUMMARY_CHILD_JOB_TIMEOUT_SEC", "20") or 20.0)
            except Exception:
                return 20.0

        sch._parent_timeout_sec = _parent_timeout_sec
        sch._child_timeout_sec = _child_timeout_sec
        logger.warning(
            "[SUMMARY PARALLEL TIMEOUT RELIEF] scheduler forced main 1m-only run_flags=(1,0,0) child_timeout=%s parent_timeout=%s version=%s",
            os.getenv("SUMMARY_CHILD_JOB_TIMEOUT_SEC", "20"),
            os.getenv("SUMMARY_PARENT_TICK_TIMEOUT_SEC", "30"),
            VERSION,
        )
        return True
    except Exception:
        logger.exception("[SUMMARY PARALLEL TIMEOUT RELIEF] scheduler 1m-only patch failed version=%s", VERSION)
        return False


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    if not _is_main_py():
        logger.warning("[SUMMARY PARALLEL TIMEOUT RELIEF] skipped non-main context version=%s", VERSION)
        return False

    try:
        changed: dict[str, tuple[str | None, str]] = {}

        # Main process: keep entry judgement light.  Heavy 3m/5m work is handled
        # by raw fallback/cache and database-side processes.
        for name, value in {
            "SUMMARY_PUSH_BG_ALL_INTERVALS": "0",
            "SUMMARY_PUSH_BG_LONG_INTERVALS": "0",
            "SUMMARY_PUSH_DISPLAY_ALL_INTERVALS": "0",
            "SUMMARY_PUSH_BG_INTERVAL_WORKERS": "1",
            "SUMMARY_PUSH_FORCE_1_3_5": "0",
            "SUMMARY_PARALLEL_FORCE_1_3_5": "0",
            "SUMMARY_PARALLEL_MAIN_ENTRY_ONLY": "1",
            "SUMMARY_PARALLEL_TIMEOUT_SEC": "20",
            "SUMMARY_PARALLEL_MIN_TIMEOUT_SEC": "20",
            "SUMMARY_PARALLEL_PARENT_TIMEOUT_SEC": "30",
            "SUMMARY_PARENT_TICK_TIMEOUT_SEC": "30",
            "SUMMARY_CHILD_JOB_TIMEOUT_SEC": "20",
            "SUMMARY_RUN_ENTRY_ON_1M_ONLY": "1",
            "SUMMARY_RANKING_PARALLEL_ENABLED": "0",
            "SUMMARY_MAIN_PUSH_1M_ONLY": "1",
        }.items():
            old, new = _force(name, value)
            if old != new:
                changed[name] = (old, new)

        # Allow cached/raw fallback to satisfy MTF consumers without forcing a
        # slow synchronous 3m/5m push computation on each main.py tick.
        for name, value in {
            "SUMMARY_MTF_PUSH_RAW_FALLBACK_ENABLED": "1",
            "SUMMARY_MTF_DIFF_FROM_1M_ENABLED": "1",
            "SUMMARY_MTF_DIFF_ALLOW_PARTIAL_BAR": "0",
            "SUMMARY_MTF_DIFF_HISTORY_ROWS": "74",
            "SUMMARY_LATEST_PREFER_HEALTH": "1",
        }.items():
            if _set_default(name, value):
                changed[name] = (None, value)

        module_patched = _patch_summary_parallel_module()
        scheduler_patched = _patch_scheduler_module()
        _INSTALLED = True
        logger.warning(
            "[SUMMARY PARALLEL TIMEOUT RELIEF] installed version=%s main_py=True module_patched=%s scheduler_patched=%s changed=%s",
            VERSION,
            module_patched,
            scheduler_patched,
            {k: v[1] for k, v in changed.items()},
        )
        return True
    except Exception:
        logger.exception("[SUMMARY PARALLEL TIMEOUT RELIEF] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[SUMMARY PARALLEL TIMEOUT RELIEF] auto install failed")

__all__ = ["VERSION", "install"]
