# ============================================================
# File   : core/startup/scheduler_exit_bootstrap.py
# Version: FINAL-PRODUCTION-REV1.0-SCHEDULER-EXIT-BOOTSTRAP
# ------------------------------------------------------------
# 【概要】
#   EXIT order sender 接続と EXIT loop scheduler 登録。
# ============================================================

from __future__ import annotations

import datetime as dt
import logging

import schedule

from global_state import global_data
from core.startup.scheduler_helpers import has_schedule_tag, log_scheduler_snapshot
from core.startup.scheduler_market_guard import is_market_time_for_exit

logger = logging.getLogger(__name__)


def install_exit_order_sender_safe() -> bool:
    logger.info("[startup.scheduler_startup] exit order sender install start")
    try:
        from trading.exit.order_sender import install_exit_order_sender
        ok = bool(install_exit_order_sender())
        try:
            global_data.exit_order_sender_installed = ok
            global_data.exit_order_sender_installed_at = dt.datetime.now()
            global_data.exit_order_sender_install_failed = not ok
        except Exception:
            pass
        if ok:
            logger.info("[startup.scheduler_startup] exit order sender installed")
        else:
            logger.warning("[startup.scheduler_startup] exit order sender install returned False")
        return ok
    except Exception as e:
        try:
            global_data.exit_order_sender_installed = False
            global_data.exit_order_sender_install_failed = True
            global_data.exit_order_sender_install_error = str(e)
            global_data.exit_order_sender_installed_at = dt.datetime.now()
        except Exception:
            pass
        logger.exception("[startup.scheduler_startup] exit order sender install failed")
        return False


def run_exit_loop_market_guarded() -> None:
    try:
        if not is_market_time_for_exit():
            logger.info("[EXIT SCHEDULER] market closed skip")
            return
        try:
            from trading.exit.exit_loop import exit_loop_5s
        except Exception:
            logger.exception("[EXIT SCHEDULER] import failed: trading.exit.exit_loop.exit_loop_5s")
            return
        logger.info("[EXIT SCHEDULER] exit_loop_5s start")
        ret = exit_loop_5s()
        logger.info("[EXIT SCHEDULER] exit_loop_5s done ret=%s", ret)
    except Exception:
        logger.exception("[EXIT SCHEDULER] exit loop failed")


def register_exit_loop_safe() -> bool:
    logger.info("[startup.scheduler_startup] exit loop scheduler bootstrap start")
    try:
        if has_schedule_tag("exit_loop_5s"):
            logger.info("[startup.scheduler_startup] exit loop already registered")
            try:
                global_data.exit_loop_scheduler_registered = True
                global_data.exit_loop_scheduler_failed = False
                global_data.exit_loop_scheduler_registered_at = dt.datetime.now()
            except Exception:
                pass
            return True
        schedule.every(5).seconds.do(run_exit_loop_market_guarded).tag("exit_loop_5s", "exit")
        try:
            global_data.exit_loop_scheduler_registered = True
            global_data.exit_loop_scheduler_registered_at = dt.datetime.now()
            global_data.exit_loop_scheduler_failed = False
            global_data.exit_loop_scheduler_error = ""
        except Exception:
            pass
        logger.info("[startup.scheduler_startup] exit loop scheduler registered every=5s")
        log_scheduler_snapshot("after exit loop scheduler register")
        return True
    except Exception as e:
        try:
            global_data.exit_loop_scheduler_registered = False
            global_data.exit_loop_scheduler_failed = True
            global_data.exit_loop_scheduler_error = str(e)
            global_data.exit_loop_scheduler_registered_at = dt.datetime.now()
        except Exception:
            pass
        logger.exception("[startup.scheduler_startup] exit loop scheduler register failed")
        return False


__all__ = ["install_exit_order_sender_safe", "run_exit_loop_market_guarded", "register_exit_loop_safe"]
