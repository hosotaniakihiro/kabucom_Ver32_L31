# -*- coding: utf-8 -*-
# ============================================================
# File   : core/startup/summary_parallel_timeout_relief_patch.py
# Version: V1-SUMMARY-PARALLEL-TIMEOUT-RELIEF-MAIN-LIGHT
# ------------------------------------------------------------
# Purpose:
#   Reduce main.py entry-tick latency and avoid repeated logs like:
#       [SUMMARY PARALLEL] tick timeout ... timeout=25.0s done=0 total=3
#
#   main.py should prioritize fresh 1m PUSH judgement.  3m/5m PUSH MTF can be
#   supplied by raw fallback/cache, while main_database.py owns heavy DB save.
# ============================================================
from __future__ import annotations

import logging
import os
import sys
from typing import Any

logger = logging.getLogger(__name__)
VERSION = "V1-SUMMARY-PARALLEL-TIMEOUT-RELIEF-MAIN-LIGHT"
_INSTALLED = False


def _is_main_py() -> bool:
    try:
        argv = " ".join(str(x).replace("\\", "/").lower() for x in sys.argv)
        return "main.py" in argv and "main_database.py" not in argv
    except Exception:
        return False


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
    """If the parallel runtime patch is already imported, tune its module attrs/env.

    The target implementation is intentionally evolving, so this patch avoids
    relying on a single function name.  It sets env values used by the patch and
    updates common constant-like attributes if they exist.
    """
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
        "SUMMARY_PARALLEL_TIMEOUT_SEC": "12",
        "SUMMARY_PARALLEL_MIN_TIMEOUT_SEC": "12",
        "SUMMARY_PARALLEL_PARENT_TIMEOUT_SEC": "18",
        "SUMMARY_PARENT_TICK_TIMEOUT_SEC": "18",
        "SUMMARY_CHILD_JOB_TIMEOUT_SEC": "12",
    }.items():
        _force(name, value)
        patched = True

    # Best-effort direct attr tuning for current/future implementations.
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
                setattr(target, attr, 12.0 if "PARENT" not in attr else 18.0)
                patched = True
        except Exception:
            pass
    return patched


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
            "SUMMARY_PARALLEL_TIMEOUT_SEC": "12",
            "SUMMARY_PARALLEL_MIN_TIMEOUT_SEC": "12",
            "SUMMARY_PARALLEL_PARENT_TIMEOUT_SEC": "18",
            "SUMMARY_PARENT_TICK_TIMEOUT_SEC": "18",
            "SUMMARY_CHILD_JOB_TIMEOUT_SEC": "12",
            "SUMMARY_RUN_ENTRY_ON_1M_ONLY": "1",
            "SUMMARY_RANKING_PARALLEL_ENABLED": "0",
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
        _INSTALLED = True
        logger.warning(
            "[SUMMARY PARALLEL TIMEOUT RELIEF] installed version=%s main_py=True module_patched=%s changed=%s",
            VERSION,
            module_patched,
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
