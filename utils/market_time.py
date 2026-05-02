# ============================================================
# File   : utils/market_time.py
# Version: PRODUCTION-STABLE-REV2.0-MARKET-SESSION-CUTOFF
# ------------------------------------------------------------
# 【概要】
#   日本株市場時間ユーティリティ
#
# 【目的】
#   - is_market_open だけでは昼休みと大引け後を区別できない問題を修正
#   - サマリー/MTF/bootstrap/scoring 用の安全な cutoff datetime を提供
#   - 未来足 15:30 が昼休み中に混入する問題を防ぐ
#
# 【仕様】
#   - 前場      09:00 <= now <= 11:30
#   - 昼休み    11:30 < now < 12:30
#   - 後場      12:30 <= now <= 15:30
#   - 大引け後  now > 15:30
#   - 寄り前    now < 09:00
#
# 【注意】
#   - 祝日判定はここでは行わない
#   - 前営業日判定が必要な場合は calendar_utils 側で扱う
# ============================================================

from __future__ import annotations

import datetime as dt
from typing import Literal


MORNING_OPEN = dt.time(9, 0)
MORNING_CLOSE = dt.time(11, 30)

AFTERNOON_OPEN = dt.time(12, 30)
AFTERNOON_CLOSE = dt.time(15, 30)


MarketSession = Literal[
    "preopen",
    "morning",
    "lunch",
    "afternoon",
    "closed",
]


def _now(now: dt.datetime | None = None) -> dt.datetime:
    return now if now is not None else dt.datetime.now()


def get_market_session(now: dt.datetime | None = None) -> MarketSession:
    now = _now(now)
    t = now.time()

    if t < MORNING_OPEN:
        return "preopen"

    if MORNING_OPEN <= t <= MORNING_CLOSE:
        return "morning"

    if MORNING_CLOSE < t < AFTERNOON_OPEN:
        return "lunch"

    if AFTERNOON_OPEN <= t <= AFTERNOON_CLOSE:
        return "afternoon"

    return "closed"


def is_market_open(now: dt.datetime | None = None) -> bool:
    session = get_market_session(now)
    return session in {"morning", "afternoon"}


def is_market_daytime(now: dt.datetime | None = None) -> bool:
    """
    寄り前/大引け後ではなく、当日の市場時間帯にいるか。
    昼休みも True。
    """
    session = get_market_session(now)
    return session in {"morning", "lunch", "afternoon"}


def get_intraday_cutoff_datetime(now: dt.datetime | None = None) -> dt.datetime:
    """
    当日リアルタイム処理で使ってよい最大 datetime を返す。

    例:
      08:30 -> 当日 09:00 より前なので now.floor(min)
      10:12 -> 当日 10:12
      12:24 -> 当日 11:30
      13:10 -> 当日 13:10
      16:00 -> 当日 15:30
    """
    now = _now(now).replace(second=0, microsecond=0)
    d = now.date()
    session = get_market_session(now)

    if session == "preopen":
        return now

    if session == "morning":
        return now

    if session == "lunch":
        return dt.datetime.combine(d, MORNING_CLOSE)

    if session == "afternoon":
        return now

    return dt.datetime.combine(d, AFTERNOON_CLOSE)


def is_future_bar(bar_dt: dt.datetime, now: dt.datetime | None = None) -> bool:
    cutoff = get_intraday_cutoff_datetime(now)
    return bar_dt.replace(tzinfo=None) > cutoff