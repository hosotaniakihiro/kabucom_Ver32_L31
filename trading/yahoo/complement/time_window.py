from __future__ import annotations
import datetime as dt
from .constants import YAHOO_REFLECT_DELAY_MINUTES

def today_str(target_date: dt.date | None = None) -> str:
    return (target_date or dt.date.today()).strftime("%Y%m%d")

def normalize_trade_date_to_yyyymmdd(v: str | dt.date | dt.datetime | None) -> str:
    if isinstance(v, dt.datetime): return v.strftime("%Y%m%d")
    if isinstance(v, dt.date): return v.strftime("%Y%m%d")
    if v:
        s = str(v).strip().replace("-", "")
        if len(s) == 8 and s.isdigit(): return s
    return dt.datetime.now().strftime("%Y%m%d")

def yyyymmdd_to_date(yyyymmdd: str) -> dt.date:
    return dt.datetime.strptime(yyyymmdd, "%Y%m%d").date()

def trade_date_hyphen(yyyymmdd: str) -> str:
    return f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:8]}"

def resolve_yahoo_reflect_end_dt(*, target_date: str | dt.date | dt.datetime, delay_minutes: int = YAHOO_REFLECT_DELAY_MINUTES) -> str:
    ymd = normalize_trade_date_to_yyyymmdd(target_date)
    day = yyyymmdd_to_date(ymd)
    now = dt.datetime.now()
    market_open = dt.datetime.combine(day, dt.time(9, 0))
    morning_close = dt.datetime.combine(day, dt.time(11, 30))
    afternoon_open = dt.datetime.combine(day, dt.time(12, 30))
    market_close = dt.datetime.combine(day, dt.time(15, 30))
    if now.date() > day: return market_close.strftime("%Y-%m-%d %H:%M:%S")
    if now.date() < day: return market_open.strftime("%Y-%m-%d %H:%M:%S")
    delayed = (now - dt.timedelta(minutes=int(delay_minutes))).replace(second=0, microsecond=0)
    if delayed < market_open: return market_open.strftime("%Y-%m-%d %H:%M:%S")
    if market_open <= delayed <= morning_close: return delayed.strftime("%Y-%m-%d %H:%M:%S")
    if morning_close < delayed < afternoon_open: return morning_close.strftime("%Y-%m-%d %H:%M:%S")
    if afternoon_open <= delayed <= market_close: return delayed.strftime("%Y-%m-%d %H:%M:%S")
    return market_close.strftime("%Y-%m-%d %H:%M:%S")
__all__ = ["today_str", "normalize_trade_date_to_yyyymmdd", "yyyymmdd_to_date", "trade_date_hyphen", "resolve_yahoo_reflect_end_dt"]
