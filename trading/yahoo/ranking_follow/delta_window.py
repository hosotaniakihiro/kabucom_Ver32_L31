# ============================================================
# File   : trading/yahoo/ranking_follow/delta_window.py
# Version: PRODUCTION-STABLE-YAHOO-RANKING-FOLLOW-WINDOW-REV1.0
# ------------------------------------------------------------
# Purpose:
#   Yahoo取得・サマリー計算の差分範囲を決める。
# ============================================================

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Optional

import pandas as pd

YAHOO_DELAY_MINUTES = 20
MARKET_AM_START = dt.time(9, 0)
MARKET_AM_END = dt.time(11, 30)
MARKET_PM_START = dt.time(12, 30)
MARKET_PM_END = dt.time(15, 30)


@dataclass(frozen=True)
class DeltaWindow:
    start: Optional[pd.Timestamp]
    end: Optional[pd.Timestamp]
    reason: str = ""

    @property
    def valid(self) -> bool:
        return self.start is not None and self.end is not None and self.start <= self.end


def floor_minute(value: dt.datetime | pd.Timestamp) -> pd.Timestamp:
    return pd.Timestamp(value).floor("min")


def market_close_dt(trade_date: dt.date | str) -> pd.Timestamp:
    d = pd.Timestamp(trade_date).date()
    return pd.Timestamp(dt.datetime.combine(d, MARKET_PM_END))


def yahoo_eligible_until(now: Optional[dt.datetime] = None, *, delay_minutes: int = YAHOO_DELAY_MINUTES) -> pd.Timestamp:
    now_ts = floor_minute(now or dt.datetime.now())
    eligible = now_ts - pd.Timedelta(minutes=delay_minutes)
    close = market_close_dt(now_ts.date())
    if eligible > close:
        eligible = close
    return eligible.floor("min")


def next_minute_after(value: Optional[object], default_start: pd.Timestamp) -> pd.Timestamp:
    if value is None or pd.isna(value):
        return default_start.floor("min")
    ts = pd.to_datetime(value, errors="coerce")
    if pd.isna(ts):
        return default_start.floor("min")
    return pd.Timestamp(ts).floor("min") + pd.Timedelta(minutes=1)


def make_download_window(
    *,
    trade_date: dt.date | str,
    last_yahoo_downloaded_at: Optional[object],
    now: Optional[dt.datetime] = None,
    delay_minutes: int = YAHOO_DELAY_MINUTES,
) -> DeltaWindow:
    d = pd.Timestamp(trade_date).date()
    default_start = pd.Timestamp(dt.datetime.combine(d, MARKET_AM_START))
    start = next_minute_after(last_yahoo_downloaded_at, default_start)
    end = yahoo_eligible_until(now, delay_minutes=delay_minutes)
    if end.date() != d:
        end = market_close_dt(d)
    if start > end:
        return DeltaWindow(None, None, reason=f"already_downloaded start={start} end={end}")
    return DeltaWindow(start, end, reason="ok")


def make_summary_window(
    *,
    trade_date: dt.date | str,
    last_summary_calculated_at: Optional[object],
    yahoo_latest_at: Optional[object],
) -> DeltaWindow:
    d = pd.Timestamp(trade_date).date()
    default_start = pd.Timestamp(dt.datetime.combine(d, MARKET_AM_START))
    start = next_minute_after(last_summary_calculated_at, default_start)
    end = pd.to_datetime(yahoo_latest_at, errors="coerce")
    if pd.isna(end):
        return DeltaWindow(None, None, reason="no_yahoo_latest")
    end = pd.Timestamp(end).floor("min")
    if end.date() != d:
        end = market_close_dt(d)
    if start > end:
        return DeltaWindow(None, None, reason=f"already_calculated start={start} end={end}")
    return DeltaWindow(start, end, reason="ok")
