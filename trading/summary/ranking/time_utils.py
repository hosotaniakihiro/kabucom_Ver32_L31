# ============================================================
# File   : trading/summary/ranking/time_utils.py
# Ver    : PRODUCTION-STABLE-RANKING-TIME-UTILS-V1.0
#          -RANKING-ONLY
#          -SESSION-LIMITED
# ------------------------------------------------------------
# ✔ RANKING定時計算用の時刻 helper
# ✔ 1m / 3m / 5m の毎時0分起点判定
# ✔ 市場時間内だけ実行対象
# ✔ 昼休み / 引け後 / 休場日でも表示 slot を解決
# ✔ PUSH系依存なし
# ============================================================

from __future__ import annotations

import datetime as dt
from typing import Optional

import pandas as pd


# ============================================================
# now / basic
# ============================================================

def now_naive() -> dt.datetime:
    return dt.datetime.now().replace(tzinfo=None, microsecond=0)


def today_date() -> dt.date:
    return now_naive().date()


def _to_naive(now: Optional[dt.datetime]) -> dt.datetime:
    if now is None:
        return now_naive()

    if isinstance(now, pd.Timestamp):
        try:
            now = now.to_pydatetime()
        except Exception:
            pass

    if getattr(now, "tzinfo", None) is not None:
        try:
            now = now.astimezone(dt.timezone.utc).replace(tzinfo=None)
        except Exception:
            now = now.replace(tzinfo=None)

    return now.replace(microsecond=0)


# ============================================================
# market session
# ============================================================

def is_weekend(now: Optional[dt.datetime] = None) -> bool:
    now = _to_naive(now)
    return now.weekday() >= 5


def is_lunch_break(now: Optional[dt.datetime] = None) -> bool:
    now = _to_naive(now)
    t = now.time()
    return dt.time(11, 30) <= t < dt.time(12, 30)


def is_morning_session(now: Optional[dt.datetime] = None) -> bool:
    now = _to_naive(now)
    t = now.time()
    return dt.time(9, 0) <= t < dt.time(11, 30)


def is_afternoon_session(now: Optional[dt.datetime] = None) -> bool:
    now = _to_naive(now)
    t = now.time()
    return dt.time(12, 30) <= t <= dt.time(15, 30)


def is_market_session(now: Optional[dt.datetime] = None) -> bool:
    now = _to_naive(now)
    if is_weekend(now):
        return False
    return is_morning_session(now) or is_afternoon_session(now)


# ============================================================
# interval helpers
# ============================================================

def floor_to_interval(now: Optional[dt.datetime], interval: int) -> dt.datetime:
    now = _to_naive(now)
    interval = max(1, int(interval))
    minute = (now.minute // interval) * interval
    return now.replace(minute=minute, second=0, microsecond=0)


def _is_target_minute(now: dt.datetime, interval: int) -> bool:
    interval = int(interval)
    if interval <= 1:
        return True
    return (now.minute % interval) == 0


def resolve_target_intervals(now: Optional[dt.datetime] = None) -> list[int]:
    """
    市場時間内だけ 1/3/5 分足の対象 interval を返す。
    毎時0分起点。
    """
    now = _to_naive(now)

    if not is_market_session(now):
        return []

    targets: list[int] = [1]

    if _is_target_minute(now, 3):
        targets.append(3)

    if _is_target_minute(now, 5):
        targets.append(5)

    return targets


# ============================================================
# display slot
# ============================================================

def _previous_business_anchor(now: dt.datetime) -> dt.datetime:
    d = now.date()
    while True:
        d = d - dt.timedelta(days=1)
        if d.weekday() < 5:
            return dt.datetime.combine(d, dt.time(15, 30))


def resolve_display_slot(interval: int, now: Optional[dt.datetime] = None) -> tuple[dt.datetime, dt.datetime]:
    """
    戻り値:
      (display_anchor_now, slot_dt)

    slot_dt:
      - セッション中: interval ごとに floor
      - 昼休み: 11:30 固定
      - 引け後: 15:30 固定
      - 9:00 前 / 休場日: 前営業日 15:30 固定
    """
    now = _to_naive(now)
    interval = max(1, int(interval))

    if is_weekend(now):
        slot_dt = _previous_business_anchor(now)
        return now, slot_dt

    t = now.time()

    if t < dt.time(9, 0):
        slot_dt = _previous_business_anchor(now)
        return now, slot_dt

    if dt.time(9, 0) <= t < dt.time(11, 30):
        slot_dt = floor_to_interval(now, interval)
        return now, slot_dt

    if dt.time(11, 30) <= t < dt.time(12, 30):
        slot_dt = now.replace(hour=11, minute=30, second=0, microsecond=0)
        return now, slot_dt

    if dt.time(12, 30) <= t <= dt.time(15, 30):
        slot_dt = floor_to_interval(now, interval)
        return now, slot_dt

    slot_dt = now.replace(hour=15, minute=30, second=0, microsecond=0)
    return now, slot_dt