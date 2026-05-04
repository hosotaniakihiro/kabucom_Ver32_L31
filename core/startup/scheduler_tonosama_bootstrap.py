# ============================================================
# File   : core/startup/scheduler_tonosama_bootstrap.py
# Version: FINAL-PRODUCTION-REV1.0-SCHEDULER-TONOSAMA-BOOTSTRAP
# ------------------------------------------------------------
# 【概要】
#   殿様イナゴENTRY scheduler 登録。
# ============================================================

from __future__ import annotations

import datetime as dt
import logging

import schedule

from global_state import global_data
from core.startup.scheduler_helpers import has_schedule_tag, log_scheduler_snapshot
from core.startup.scheduler_market_guard import is_market_time_for_entry

logger = logging.getLogger(__name__)


def run_tonosama_entry_market_guarded() -> None:
    try:
        if not is_market_time_for_entry():
            logger.info("[TONOSAMA ENTRY SCHEDULER] market closed skip")
            return
        try:
            from trading.entry.tonosama_master_ai import tonosama_loop
        except Exception:
            logger.exception("[TONOSAMA ENTRY SCHEDULER] import failed: tonosama_loop")
            return
        if not callable(tonosama_loop):
            logger.warning("[TONOSAMA ENTRY SCHEDULER] tonosama_loop not callable")
            return
        logger.info("[TONOSAMA ENTRY SCHEDULER] tonosama_loop start")
        ret = tonosama_loop()
        logger.info("[TONOSAMA ENTRY SCHEDULER] tonosama_loop done ret=%s", ret)
    except Exception:
        logger.exception("[TONOSAMA ENTRY SCHEDULER] tonosama entry loop failed")


def register_tonosama_entry_scheduler_safe() -> bool:
    logger.info("[startup.scheduler_startup] tonosama entry scheduler bootstrap start")
    try:
        if has_schedule_tag("tonosama_entry"):
            logger.info("[startup.scheduler_startup] tonosama entry scheduler already registered")
            try:
                global_data.tonosama_entry_scheduler_registered = True
                global_data.tonosama_entry_scheduler_failed = False
                global_data.tonosama_entry_scheduler_registered_at = dt.datetime.now()
            except Exception:
                pass
            return True
        registered_by_module = False
        try:
            from trading.entry.tonosama_master_ai import register_tonosama_scheduler
            if callable(register_tonosama_scheduler):
                register_tonosama_scheduler(schedule)
                registered_by_module = True
                logger.info("[startup.scheduler_startup] tonosama entry scheduler registered by tonosama_master_ai")
        except Exception:
            logger.warning("[startup.scheduler_startup] tonosama_master_ai.register_tonosama_scheduler failed; fallback register", exc_info=True)
        if not registered_by_module:
            schedule.every(15).seconds.do(run_tonosama_entry_market_guarded).tag("tonosama_entry", "entry")
            logger.info("[startup.scheduler_startup] tonosama entry scheduler registered fallback every=15s")
        try:
            global_data.tonosama_entry_scheduler_registered = True
            global_data.tonosama_entry_scheduler_registered_by_module = bool(registered_by_module)
            global_data.tonosama_entry_scheduler_registered_at = dt.datetime.now()
            global_data.tonosama_entry_scheduler_failed = False
            global_data.tonosama_entry_scheduler_error = ""
        except Exception:
            pass
        logger.info("[startup.scheduler_startup] tonosama entry scheduler registered by_module=%s", registered_by_module)
        log_scheduler_snapshot("after tonosama entry scheduler register")
        return True
    except Exception as e:
        try:
            global_data.tonosama_entry_scheduler_registered = False
            global_data.tonosama_entry_scheduler_failed = True
            global_data.tonosama_entry_scheduler_error = str(e)
            global_data.tonosama_entry_scheduler_registered_at = dt.datetime.now()
        except Exception:
            pass
        logger.exception("[startup.scheduler_startup] tonosama entry scheduler register failed")
        return False


__all__ = ["run_tonosama_entry_market_guarded", "register_tonosama_entry_scheduler_safe"]
