# ============================================================
# File   : core/startup/scheduler_startup.py
# Version: FINAL-PRODUCTION-REV24.0-THIN-SCHEDULER-STARTUP
# ------------------------------------------------------------
# 【概要】
#   scheduler 起動スタックの薄い入口。
# ============================================================

from __future__ import annotations

import datetime as dt
import logging

from global_state import global_data
from core.startup.scheduler_bootstrap import register_scheduler_safe
from core.startup.schedule_loop import start_schedule_run_pending_loop_safe, get_schedule_loop_status
from core.startup.scheduler_helpers import safe_schedule_snapshot, log_scheduler_snapshot, has_schedule_tag
from core.startup.scheduler_market_guard import (
    is_market_time,
    is_market_time_for_exit,
    is_market_time_for_entry,
    _is_market_time,
    _is_market_time_for_exit,
    _is_market_time_for_entry,
)
from core.startup.scheduler_exit_bootstrap import (
    install_exit_order_sender_safe,
    run_exit_loop_market_guarded,
    register_exit_loop_safe,
)
from core.startup.scheduler_tonosama_bootstrap import (
    run_tonosama_entry_market_guarded,
    register_tonosama_entry_scheduler_safe,
)
from core.startup.scheduler_ranking_bootstrap import start_ranking_db_writer_safe
from core.startup.scheduler_summary_debug import run_summary_tick_once_debug_safe

logger = logging.getLogger(__name__)

VERSION = "FINAL-PRODUCTION-REV24.0-THIN-SCHEDULER-STARTUP"


def register_scheduler_early_safe() -> bool:
    logger.info("🕒 scheduler bootstrap early start before startup_summary_restore")
    log_scheduler_snapshot("before early scheduler bootstrap")
    ok = False
    try:
        register_scheduler_safe()
        ok = True
    except Exception:
        logger.exception("❌ scheduler bootstrap early failed")
        ok = False
    try:
        global_data.scheduler_bootstrap_registered = bool(ok)
        global_data.scheduler_bootstrap_registered_at = dt.datetime.now() if ok else None
        global_data.scheduler_bootstrap_failed = not bool(ok)
        global_data.scheduler_bootstrap_result = {"ok": bool(ok), "phase": "before_startup_summary_restore"}
    except Exception:
        pass
    log_scheduler_snapshot("after early scheduler bootstrap")
    if ok:
        logger.info("✅ scheduler bootstrap early complete")
    else:
        logger.warning("⚠ scheduler bootstrap early completed ok=False")
    return bool(ok)


def register_scheduler_fallback_safe() -> bool:
    try:
        already = bool(getattr(global_data, "scheduler_bootstrap_registered", False))
    except Exception:
        already = False
    if already:
        logger.info("🕒 scheduler bootstrap fallback skipped: already registered")
        log_scheduler_snapshot("scheduler fallback skipped")
        return True
    logger.info("🕒 scheduler bootstrap fallback start")
    ok = False
    try:
        register_scheduler_safe()
        ok = True
    except Exception:
        logger.exception("❌ scheduler bootstrap fallback failed")
        ok = False
    try:
        global_data.scheduler_bootstrap_registered = bool(ok)
        global_data.scheduler_bootstrap_registered_at = dt.datetime.now() if ok else None
        global_data.scheduler_bootstrap_failed = not bool(ok)
    except Exception:
        pass
    log_scheduler_snapshot("after scheduler fallback")
    return bool(ok)


def start_schedule_loop_early_safe() -> bool:
    logger.info("🕒 schedule run_pending loop start after scheduler bootstrap")
    try:
        ok = start_schedule_run_pending_loop_safe(interval_seconds=0.5, heartbeat_seconds=30.0, snapshot_limit=30)
    except Exception:
        logger.exception("❌ schedule run_pending loop start failed")
        ok = False
    logger.info("🕒 schedule run_pending loop result ok=%s status=%s", ok, get_schedule_loop_status())
    return bool(ok)


def ensure_schedule_loop_running_safe() -> bool:
    try:
        loop_status = get_schedule_loop_status()
        if not bool(loop_status.get("running")):
            logger.warning("🕒 schedule loop fallback start because not running status=%s", loop_status)
            return start_schedule_loop_early_safe()
        logger.info("🕒 schedule loop fallback skipped: already running status=%s", loop_status)
        return True
    except Exception:
        logger.exception("❌ schedule loop fallback check failed")
        return False


def start_scheduler_stack_before_restore() -> None:
    register_scheduler_early_safe()
    install_exit_order_sender_safe()
    register_exit_loop_safe()
    register_tonosama_entry_scheduler_safe()
    start_ranking_db_writer_safe()
    start_schedule_loop_early_safe()
    run_summary_tick_once_debug_safe()


__all__ = [
    "VERSION",
    "safe_schedule_snapshot", "log_scheduler_snapshot", "has_schedule_tag",
    "is_market_time", "is_market_time_for_exit", "is_market_time_for_entry",
    "_is_market_time", "_is_market_time_for_exit", "_is_market_time_for_entry",
    "install_exit_order_sender_safe", "run_exit_loop_market_guarded", "register_exit_loop_safe",
    "run_tonosama_entry_market_guarded", "register_tonosama_entry_scheduler_safe",
    "start_ranking_db_writer_safe",
    "register_scheduler_early_safe", "register_scheduler_fallback_safe",
    "start_schedule_loop_early_safe", "ensure_schedule_loop_running_safe",
    "run_summary_tick_once_debug_safe",
    "start_scheduler_stack_before_restore",
]
