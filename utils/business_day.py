# ============================================================
# business_day_utils.py（Ver21 安定版）
# ------------------------------------------------------------
# ・内閣府の祝日CSVを自動取得（毎日更新）
# ・祝日キャッシュ（当日5時にリフレッシュ）
# ・前営業日・次営業日を高速・安全に判定
# ・ネットワーク遮断時はローカルキャッシュ fallback
# ============================================================

import datetime as dt
import requests
import pandas as pd
import logging
import os

logger = logging.getLogger(__name__)

HOLIDAY_URL = "https://www8.cao.go.jp/chosei/shukujitsu/syukujitsu.csv"
LOCAL_CACHE_FILE = "holidays_cache.csv"

_cache_holidays = set()
_cache_date = None
_cache_loaded_time = None


# ------------------------------------------------------------
# 🔹 祝日データの取得（自動キャッシュ）
# ------------------------------------------------------------
def _load_holidays():
    global _cache_holidays, _cache_date, _cache_loaded_time

    today = dt.date.today()

    # 5時までは前日のキャッシュを使う（設定可能）
    now = dt.datetime.now()
    refresh_needed = (
        _cache_date != today or
        _cache_loaded_time is None or
        now.hour >= 5 and _cache_date != today
    )

    if not refresh_needed:
        return

    try:
        logger.info("🕊️ 祝日CSVをダウンロード中…")
        df = pd.read_csv(HOLIDAY_URL, encoding="shift_jis")
        df["国民の祝日・休日月日"] = pd.to_datetime(df["国民の祝日・休日月日"])
        holidays = set(df["国民の祝日・休日月日"].dt.date.tolist())

        # キャッシュ更新
        _cache_holidays = holidays
        _cache_date = today
        _cache_loaded_time = now

        # ローカルにも保存
        df.to_csv(LOCAL_CACHE_FILE, index=False)
        logger.info(f"✅ 祝日データ更新（{len(holidays)} 件）")

    except Exception as e:
        logger.warning(f"⚠️ 祝日取得失敗 → ローカルキャッシュにfallback: {e}")

        if os.path.exists(LOCAL_CACHE_FILE):
            try:
                df = pd.read_csv(LOCAL_CACHE_FILE)
                df["国民の祝日・休日月日"] = pd.to_datetime(df["国民の祝日・休日月日"])
                holidays = set(df["国民の祝日・休日月日"].dt.date.tolist())

                _cache_holidays = holidays
                _cache_date = today
                _cache_loaded_time = now

                logger.info(f"📦 ローカルキャッシュ読み込み成功（{len(holidays)} 件）")
            except Exception as e2:
                logger.error(f"❌ ローカルキャッシュ読み込み失敗: {e2}")
                _cache_holidays = set()
        else:
            logger.error("❌ 祝日キャッシュが存在しません")
            _cache_holidays = set()


# ------------------------------------------------------------
# 🔹 営業日判定
# ------------------------------------------------------------
def is_business_day(date: dt.date) -> bool:
    _load_holidays()
    return date.weekday() < 5 and date not in _cache_holidays


# ------------------------------------------------------------
# 🔹 前営業日を取得
# ------------------------------------------------------------
def get_prev_business_day(date: dt.date) -> dt.date:
    _load_holidays()
    d = date - dt.timedelta(days=1)
    while not is_business_day(d):
        d -= dt.timedelta(days=1)
    return d


# ------------------------------------------------------------
# 🔹 前々営業日を取得
# ------------------------------------------------------------
def get_prev_prev_business_day(date: dt.date) -> dt.date:
    d = get_prev_business_day(date)
    return get_prev_business_day(d)


# ------------------------------------------------------------
# 🔹 次営業日を取得
# ------------------------------------------------------------
def get_next_business_day(date: dt.date) -> dt.date:
    _load_holidays()
    d = date + dt.timedelta(days=1)
    while not is_business_day(d):
        d += dt.timedelta(days=1)
    return d


# ------------------------------------------------------------
# 🔹 今日が営業日か？
# ------------------------------------------------------------
def is_today_business_day() -> bool:
    return is_business_day(dt.date.today())


# ------------------------------------------------------------
# 🔹 今日が祝日 / 土日判定
# ------------------------------------------------------------
def is_holiday(date: dt.date) -> bool:
    _load_holidays()
    return date in _cache_holidays


def is_weekend(date: dt.date) -> bool:
    return date.weekday() >= 5


# ------------------------------------------------------------
# 🔹 営業日レンジ作成
# ------------------------------------------------------------
def get_business_days(start: dt.date, end: dt.date) -> list:
    _load_holidays()
    days = []
    cur = start
    while cur <= end:
        if is_business_day(cur):
            days.append(cur)
        cur += dt.timedelta(days=1)
    return days


# ------------------------------------------------------------
# 🔹 明日が営業日か？
# ------------------------------------------------------------
def is_tomorrow_business_day() -> bool:
    return is_business_day(dt.date.today() + dt.timedelta(days=1))


# ------------------------------------------------------------
# 🔹 市場時間（場中）判定
# ------------------------------------------------------------
def is_market_open(now: dt.datetime = None) -> bool:
    """
    日本株市場の取引時間:
      - 午前 09:00～11:30
      - 午後 12:30～15:30
    """
    if now is None:
        now = dt.datetime.now()

    t = now.time()

    am_start = dt.time(9, 0)
    am_end   = dt.time(11, 30)

    pm_start = dt.time(12, 30)
    pm_end   = dt.time(15, 30)

    return (am_start <= t <= am_end) or (pm_start <= t <= pm_end)
