# ============================================================
# business_day_utils.py
# Version: Ver23.0-FINAL-PRODUCTION-STABLE-CACHE-FIRST
# ------------------------------------------------------------
# ・内閣府祝日CSV自動取得
# ・起動時はローカルキャッシュ優先
# ・祝日CSVを毎回ダウンロードしない
# ・キャッシュが古い場合のみ更新
# ・営業日高速判定
# ・前営業日 / 次営業日 API完全互換
# ・ネットワーク遮断時ローカルfallback
# ・get_previous_business_day 互換維持
# ・get_last_market_close_datetime 完全維持
# ・is_market_open 営業日連動化
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
from pathlib import Path
from typing import Optional

import pandas as pd
import requests

logger = logging.getLogger(__name__)

HOLIDAY_URL = "https://www8.cao.go.jp/chosei/shukujitsu/syukujitsu.csv"

# 既存互換のためファイル名は維持
LOCAL_CACHE_FILE = "holidays_cache.csv"

# キャッシュ更新頻度
# 祝日は毎回更新不要。通常は24時間で十分。
HOLIDAY_CACHE_TTL_HOURS = 24

_cache_holidays: set[dt.date] = set()
_cache_date: Optional[dt.date] = None
_cache_loaded_time: Optional[dt.datetime] = None


# ============================================================
# path / cache helpers
# ============================================================

def _cache_path() -> Path:
    return Path(LOCAL_CACHE_FILE)


def _cache_file_exists() -> bool:
    try:
        return _cache_path().exists()
    except Exception:
        return False


def _cache_file_age_hours() -> float:
    try:
        p = _cache_path()
        if not p.exists():
            return 999999.0

        mtime = dt.datetime.fromtimestamp(p.stat().st_mtime)
        return (dt.datetime.now() - mtime).total_seconds() / 3600.0

    except Exception:
        return 999999.0


def _is_local_cache_fresh(ttl_hours: int = HOLIDAY_CACHE_TTL_HOURS) -> bool:
    """
    ローカル祝日CSVが新しいか判定する。
    """
    try:
        if not _cache_file_exists():
            return False

        return _cache_file_age_hours() <= float(ttl_hours)

    except Exception:
        return False


def _read_holidays_from_df(df: pd.DataFrame) -> set[dt.date]:
    """
    内閣府CSV / ローカルCSVから祝日date setを作る。
    """
    if df is None or df.empty:
        return set()

    date_col = None

    # 内閣府CSV
    if "国民の祝日・休日月日" in df.columns:
        date_col = "国民の祝日・休日月日"

    # 念のため英語/汎用列も許容
    elif "date" in df.columns:
        date_col = "date"
    elif "Date" in df.columns:
        date_col = "Date"

    if date_col is None:
        logger.warning(
            "[business_day_utils] holiday date column missing cols=%s",
            list(df.columns),
        )
        return set()

    s = pd.to_datetime(df[date_col], errors="coerce")
    s = s.dropna()

    return set(s.dt.date.tolist())


def _load_local_holiday_cache() -> bool:
    """
    ローカルキャッシュから祝日を読み込む。
    """
    global _cache_holidays, _cache_date, _cache_loaded_time

    p = _cache_path()

    if not p.exists():
        return False

    try:
        df = pd.read_csv(p)

        holidays = _read_holidays_from_df(df)

        if not holidays:
            logger.warning("[business_day_utils] local holiday cache empty path=%s", p)
            return False

        now = dt.datetime.now()

        _cache_holidays = holidays
        _cache_date = now.date()
        _cache_loaded_time = now

        logger.info(
            "📦 祝日ローカルキャッシュ使用（%s 件, age=%.1fh）",
            len(holidays),
            _cache_file_age_hours(),
        )
        return True

    except Exception as e:
        logger.warning("⚠️ 祝日ローカルキャッシュ読み込み失敗: %s", e, exc_info=True)
        return False


def _download_holiday_csv() -> bool:
    """
    内閣府CSVをダウンロードしてローカルキャッシュへ保存する。
    """
    global _cache_holidays, _cache_date, _cache_loaded_time

    try:
        logger.info("🕊️ 祝日CSVをダウンロード中…")

        # timeoutを付けて起動詰まりを防ぐ
        res = requests.get(HOLIDAY_URL, timeout=5)
        res.raise_for_status()

        # 内閣府CSVはshift_jis
        from io import StringIO

        text = res.content.decode("shift_jis", errors="replace")
        df = pd.read_csv(StringIO(text))

        holidays = _read_holidays_from_df(df)

        if not holidays:
            logger.warning("⚠️ 祝日CSVダウンロード成功したが祝日データが空")
            return False

        _cache_holidays = holidays
        _cache_date = dt.date.today()
        _cache_loaded_time = dt.datetime.now()

        df.to_csv(LOCAL_CACHE_FILE, index=False)

        logger.info("✅ 祝日データ更新（%s 件）", len(holidays))
        return True

    except Exception as e:
        logger.warning("⚠️ 祝日取得失敗 → ローカルキャッシュfallback: %s", e)
        return False


# ============================================================
# 祝日データ取得（自動キャッシュ）
# ============================================================

def _load_holidays(force_download: bool = False) -> None:
    """
    祝日データをロードする。

    Ver23.0:
      - 起動直後でも、ローカルキャッシュが24時間以内ならダウンロードしない
      - メモリキャッシュが当日ロード済みなら何もしない
      - キャッシュが古い場合だけダウンロードを試みる
      - ダウンロード失敗時はローカルキャッシュfallback
    """
    global _cache_holidays, _cache_date, _cache_loaded_time

    today = dt.date.today()
    now = dt.datetime.now()

    # --------------------------------------------------------
    # 1. メモリキャッシュが当日ロード済みなら何もしない
    # --------------------------------------------------------
    if (
        not force_download
        and _cache_loaded_time is not None
        and _cache_date == today
        and _cache_holidays
    ):
        return

    # --------------------------------------------------------
    # 2. 起動直後はローカルキャッシュ優先
    #    24時間以内ならダウンロードしない
    # --------------------------------------------------------
    if not force_download and _is_local_cache_fresh():
        ok = _load_local_holiday_cache()
        if ok:
            return

    # --------------------------------------------------------
    # 3. キャッシュが古い/無い場合のみダウンロード
    # --------------------------------------------------------
    ok = _download_holiday_csv()

    if ok:
        return

    # --------------------------------------------------------
    # 4. ダウンロード失敗時は古いキャッシュでも読む
    # --------------------------------------------------------
    if _load_local_holiday_cache():
        return

    # --------------------------------------------------------
    # 5. 最後の保険
    # --------------------------------------------------------
    logger.error("❌ 祝日キャッシュが存在しません。土日判定のみで営業日判定します。")
    _cache_holidays = set()
    _cache_date = today
    _cache_loaded_time = now


def refresh_holidays(force: bool = True) -> None:
    """
    外部から明示的に祝日CSVを更新したい場合のAPI。
    """
    _load_holidays(force_download=force)


# ============================================================
# 営業日判定
# ============================================================

def is_business_day(date: dt.date) -> bool:
    _load_holidays()
    return date.weekday() < 5 and date not in _cache_holidays


def is_today_business_day() -> bool:
    return is_business_day(dt.date.today())


# ============================================================
# 前営業日
# ============================================================

def get_prev_business_day(date: dt.date) -> dt.date:
    _load_holidays()

    d = date - dt.timedelta(days=1)

    while not is_business_day(d):
        d -= dt.timedelta(days=1)

    return d


def get_previous_business_day(date: dt.date | None = None) -> dt.date:
    if date is None:
        date = dt.date.today()

    return get_prev_business_day(date)


def get_prev_prev_business_day(date: dt.date) -> dt.date:
    d = get_prev_business_day(date)
    return get_prev_business_day(d)


# ============================================================
# 次営業日
# ============================================================

def get_next_business_day(date: dt.date) -> dt.date:
    _load_holidays()

    d = date + dt.timedelta(days=1)

    while not is_business_day(d):
        d += dt.timedelta(days=1)

    return d


# ============================================================
# 祝日 / 週末判定
# ============================================================

def is_holiday(date: dt.date) -> bool:
    _load_holidays()
    return date in _cache_holidays


def is_weekend(date: dt.date) -> bool:
    return date.weekday() >= 5


# ============================================================
# 営業日レンジ
# ============================================================

def get_business_days(start: dt.date, end: dt.date) -> list[dt.date]:
    _load_holidays()

    days: list[dt.date] = []
    cur = start

    while cur <= end:
        if is_business_day(cur):
            days.append(cur)
        cur += dt.timedelta(days=1)

    return days


def is_tomorrow_business_day() -> bool:
    return is_business_day(dt.date.today() + dt.timedelta(days=1))


# ============================================================
# 市場時間判定（営業日連動版）
# ============================================================

def is_market_open(now: dt.datetime | None = None) -> bool:
    """
    日本株市場取引時間:
      午前 09:00～11:30
      午後 12:30～15:30

    営業日でなければ常に False。
    """
    if now is None:
        now = dt.datetime.now()

    if not is_business_day(now.date()):
        return False

    t = now.time()

    am_start = dt.time(9, 0)
    am_end = dt.time(11, 30)

    pm_start = dt.time(12, 30)
    pm_end = dt.time(15, 30)

    return (am_start <= t <= am_end) or (pm_start <= t <= pm_end)


def is_market_time_datetime(value: dt.datetime | pd.Timestamp | str) -> bool:
    """
    任意datetimeが市場時間内か判定する。
    起動時DB読込フィルタからも使える。
    """
    try:
        ts = pd.to_datetime(value, errors="coerce")

        if pd.isna(ts):
            return False

        py_dt = ts.to_pydatetime().replace(tzinfo=None)

        return is_market_open(py_dt)

    except Exception:
        return False


# ============================================================
# 最終マーケットクローズ日時
# ============================================================

def get_last_market_close_datetime() -> dt.datetime:
    today = dt.date.today()
    now = dt.datetime.now()

    if is_business_day(today):
        if now.time() >= dt.time(15, 30):
            return dt.datetime.combine(today, dt.time(15, 30))

    prev = get_previous_business_day(today)

    return dt.datetime.combine(prev, dt.time(15, 30))


# ============================================================
# 起動時DB選択補助
# ============================================================

def get_effective_trade_date_for_startup(now: dt.datetime | None = None) -> dt.date:
    """
    起動時に使うべき取引日を返す。

    ルール:
      - 土日祝日: 直近営業日
      - 営業日 9:00前: 直近営業日
      - 営業日 9:00以降: 当日
        ※ 昼休み・15:30後も当日を使う
    """
    now = now or dt.datetime.now()
    today = now.date()

    if not is_business_day(today):
        return get_previous_business_day(today)

    if now.time() < dt.time(9, 0):
        return get_previous_business_day(today)

    return today

__all__ = [
    "HOLIDAY_URL",
    "LOCAL_CACHE_FILE",
    "HOLIDAY_CACHE_TTL_HOURS",

    "refresh_holidays",
    "is_business_day",
    "is_today_business_day",
    "get_prev_business_day",
    "get_previous_business_day",
    "get_prev_prev_business_day",
    "get_next_business_day",
    "is_holiday",
    "is_weekend",
    "get_business_days",
    "is_tomorrow_business_day",
    "is_market_open",
    "is_market_time_datetime",
    "get_last_market_close_datetime",
    "get_effective_trade_date_for_startup",
]