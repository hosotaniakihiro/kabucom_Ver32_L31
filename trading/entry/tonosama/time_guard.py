# ============================================================
# File   : trading/entry/tonosama/time_guard.py
# Version: Ver1.0-TONOSAMA-ENTRY-TIME-GUARD
# ============================================================
from __future__ import annotations
import datetime as dt
from typing import Optional

def is_market_time(now: Optional[dt.datetime] = None) -> bool:
    now = now or dt.datetime.now()
    try:
        if now.weekday() >= 5:
            return False
        t = now.time()
        return bool(dt.time(9, 0) <= t <= dt.time(11, 30) or dt.time(12, 30) <= t <= dt.time(15, 30))
    except Exception:
        return False
