# ============================================================
# File   : core/startup/scheduler_market_guard.py
# Version: FINAL-PRODUCTION-REV1.0-SCHEDULER-MARKET-GUARD
# ------------------------------------------------------------
# 【概要】
#   scheduler用の市場時間判定。
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
from typing import Optional

logger = logging.getLogger(__name__)


def is_market_time(now: Optional[dt.datetime] = None) -> bool:
    now = now or dt.datetime.now()
    try:
        if int(now.weekday()) >= 5:
            return False
        t = now.time()
        morning = dt.time(9, 0) <= t <= dt.time(11, 30)
        afternoon = dt.time(12, 30) <= t <= dt.time(15, 30)
        return bool(morning or afternoon)
    except Exception:
        logger.warning("[scheduler_startup] market time check failed -> safe skip", exc_info=True)
        return False


def is_market_time_for_exit(now: Optional[dt.datetime] = None) -> bool:
    return is_market_time(now)


def is_market_time_for_entry(now: Optional[dt.datetime] = None) -> bool:
    return is_market_time(now)


_is_market_time = is_market_time
_is_market_time_for_exit = is_market_time_for_exit
_is_market_time_for_entry = is_market_time_for_entry


__all__ = [
    "is_market_time",
    "is_market_time_for_exit",
    "is_market_time_for_entry",
    "_is_market_time",
    "_is_market_time_for_exit",
    "_is_market_time_for_entry",
]
