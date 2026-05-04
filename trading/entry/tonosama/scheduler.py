# ============================================================
# File   : trading/entry/tonosama/scheduler.py
# Version: Ver1.0-TONOSAMA-ENTRY-SCHEDULER
# ============================================================
from __future__ import annotations
import logging
from .config import SCHEDULER_INTERVAL_SEC
from .runner import tonosama_loop
logger = logging.getLogger(__name__)
_scheduler_registered = False

def register_tonosama_scheduler(schedule):
    global _scheduler_registered
    if _scheduler_registered:
        logger.warning("⚠ tonosama scheduler already registered → skip"); return
    try:
        schedule.every(SCHEDULER_INTERVAL_SEC).seconds.do(tonosama_loop).tag("tonosama_entry", "entry")
    except Exception:
        schedule.every(SCHEDULER_INTERVAL_SEC).seconds.do(tonosama_loop)
    _scheduler_registered=True
    logger.info("✅ tonosama scheduler registered (%ss)", SCHEDULER_INTERVAL_SEC)
