# ============================================================
# File   : core/startup/scheduler_summary_debug.py
# Version: FINAL-PRODUCTION-REV1.0-SCHEDULER-SUMMARY-DEBUG
# ------------------------------------------------------------
# 【概要】
#   起動直後の summary tick once debug。
# ============================================================

from __future__ import annotations

import datetime as dt
import logging

from global_state import global_data
from core.startup.startup_config import resolve_attr

logger = logging.getLogger(__name__)


def run_summary_tick_once_debug_safe() -> bool:
    logger.info("🧪 summary tick once debug start after scheduler bootstrap")
    try:
        fn = resolve_attr("scheduler_jobs.summary.scheduler", "run_summary_tick_once")
        if not callable(fn):
            logger.warning("🧪 summary tick once debug skipped: scheduler_jobs.summary.scheduler.run_summary_tick_once not found")
            try:
                global_data.summary_tick_once_debug_done = False
                global_data.summary_tick_once_debug_failed = True
                global_data.summary_tick_once_debug_result = "function_not_found"
            except Exception:
                pass
            return False
        ret = fn()
        try:
            global_data.summary_tick_once_debug_done = True
            global_data.summary_tick_once_debug_failed = False
            global_data.summary_tick_once_debug_result = ret
            global_data.summary_tick_once_debug_at = dt.datetime.now()
        except Exception:
            pass
        logger.info("✅ summary tick once debug done ret=%s", ret)
        return True
    except Exception:
        try:
            global_data.summary_tick_once_debug_done = False
            global_data.summary_tick_once_debug_failed = True
            global_data.summary_tick_once_debug_result = "exception"
        except Exception:
            pass
        logger.exception("❌ summary tick once debug failed")
        return False


__all__ = ["run_summary_tick_once_debug_safe"]
