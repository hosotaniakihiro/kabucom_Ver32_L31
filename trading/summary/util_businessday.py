# trading/summary/util_businessday.py
import datetime as dt
import jpholiday


def is_business_day(date: dt.date | None = None) -> bool:
    """指定日が営業日（土日祝でない）かどうか"""
    if date is None:
        date = dt.date.today()
    return date.weekday() < 5 and not jpholiday.is_holiday(date)


def get_last_business_day(base_date: dt.date | None = None) -> dt.date:
    """日本の前営業日を返す（土日祝対応）"""
    if base_date is None:
        base_date = dt.date.today()

    day = base_date - dt.timedelta(days=1)
    while day.weekday() >= 5 or jpholiday.is_holiday(day):
        day -= dt.timedelta(days=1)
    return day
