# trading/summary/bizday_util.py
# ============================================================
# 営業日取得ユーティリティ（日本の祝日＋土日判定）
# ============================================================

import datetime as dt
import jpholiday


def is_business_day(day: dt.date) -> bool:
    """日本の営業日（土日＋祝日を除外）"""
    return (day.weekday() < 5) and (not jpholiday.is_holiday(day))


def get_past_business_days(n=2):
    """
    今日から n 個の過去営業日を返す（例：n=2 → 前営業日 / 前々営業日）
    """
    result = []
    day = dt.date.today()

    while len(result) < n:
        day = day - dt.timedelta(days=1)
        if is_business_day(day):
            result.append(day)

    return result
