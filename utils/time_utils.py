# ============================================================
# utils/time_utils.py（Ver24-FINAL）
# ------------------------------------------------------------
# ・Yahoo 遅延境界
# ・分足境界
# ・初期サマリー cutoff
# ============================================================

import datetime as dt
import logging

logger = logging.getLogger(__name__)

YAHOO_DELAY_MINUTES = 20
MARKET_CLOSE_TIME = dt.time(15, 30)


# ------------------------------------------------------------
# Yahoo 境界時刻
# ------------------------------------------------------------
def get_yahoo_border_time(now: dt.datetime | None = None) -> dt.datetime:
    if now is None:
        now = dt.datetime.now()

    now = now.replace(second=0, microsecond=0)

    if now.time() >= MARKET_CLOSE_TIME:
        return dt.datetime.combine(now.date(), MARKET_CLOSE_TIME)

    return now - dt.timedelta(minutes=YAHOO_DELAY_MINUTES)


# ------------------------------------------------------------
# interval 分の floor
# ------------------------------------------------------------
def floor_time_to_interval(
    value: dt.datetime,
    interval_min: int
) -> dt.datetime:
    minute = (value.minute // interval_min) * interval_min
    return value.replace(minute=minute, second=0, microsecond=0)


# ------------------------------------------------------------
# 初期サマリー cutoff
# ------------------------------------------------------------
def get_initial_summary_cutoff(
    now: dt.datetime | None = None
) -> dt.datetime:
    if now is None:
        now = dt.datetime.now()

    if now.time() >= MARKET_CLOSE_TIME:
        return dt.datetime.combine(now.date(), MARKET_CLOSE_TIME)

    return now.replace(second=0, microsecond=0)
