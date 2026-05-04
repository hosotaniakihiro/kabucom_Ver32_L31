import datetime as dt
import holidays
import logging
import os
logger = logging.getLogger(__name__)
jp_holidays = holidays.JP()


from datetime import timedelta, date
import jpholiday

def last_business_day(check_date: date) -> date:
    """
    与えられた日付から直近の営業日を返す
    """
    d = check_date
    while d.weekday() >= 5 or jpholiday.is_holiday(d):
        d -= timedelta(days=1)
    return d

def is_market_open():
    if os.getenv("FORCE_MARKET_OPEN") == "true":
        return True
    """
    現在の時刻が日本の株式市場の取引時間内であるかを判断します。
    （平日 9:00 - 11:30, 12:30 - 15:00、祝日を除く）
    """
    now = dt.datetime.now()
    market_open_morning = dt.time(8, 58, 0)
    market_close_morning = dt.time(11, 30, 0)
    market_open_afternoon = dt.time(12, 30, 0)
    market_close_afternoon = dt.time(15, 30, 1)

    if now.weekday() >= 5:  # 土日
        logger.debug("市場は週末のため閉まっています。")
        return False

    if now.date() in jp_holidays:
        logger.debug("市場は祝日のため閉まっています。")
        return False

    current_time = now.time()
    if (market_open_morning <= current_time <= market_close_morning) or \
       (market_open_afternoon <= current_time <= market_close_afternoon):
        logger.debug("市場は開場中です。")
        return True
    else:
        logger.debug("市場は時間外です。")
        return False
