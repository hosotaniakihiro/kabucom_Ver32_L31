# ============================================================
# File   : scheduler_jobs/summary/calendar_utils.py
# Ver    : PRODUCTION-STABLE-SUMMARY-CALENDAR-UTILS-V1.0
#          -TIME-LOCKED-BASE-00
#          -BUSINESS-DAY-BASICS
# ------------------------------------------------------------
# ✔ 日時 helper
# ✔ 毎時0分起点の 1m / 3m / 5m 判定
# ✔ 営業日 / 前営業日 helper
# ✔ 市場時間帯 helper
# ✔ 対象日(date list) helper
# ✔ 旧 import 経路との互換を意識
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

# JP cash equity rough session constants
_PREOPEN_START = dt.time(8, 0)
_MARKET_OPEN = dt.time(9, 0)
_LUNCH_START = dt.time(11, 30)
_LUNCH_END = dt.time(12, 30)
_MARKET_CLOSE = dt.time(15, 30)


# ============================================================
# basic datetime helpers
# ============================================================

def now_dt() -> dt.datetime:
    return dt.datetime.now().replace(tzinfo=None, microsecond=0)


def today_date() -> dt.date:
    return now_dt().date()


def minute_of(x: dt.datetime) -> int:
    try:
        return int(x.minute)
    except Exception:
        return 0


def hour_of(x: dt.datetime) -> int:
    try:
        return int(x.hour)
    except Exception:
        return 0


def normalize_dates(values: Iterable[object] | object | None) -> list[dt.date]:
    """
    dt.date / dt.datetime / 'YYYY-MM-DD' / iterable を date list に正規化
    """
    if values is None:
        return []

    if isinstance(values, (dt.date, dt.datetime, str)):
        values = [values]

    out: list[dt.date] = []
    for v in values:
        try:
            if isinstance(v, dt.datetime):
                out.append(v.date())
                continue
            if isinstance(v, dt.date):
                out.append(v)
                continue
            if isinstance(v, str):
                out.append(dt.datetime.fromisoformat(v[:10]).date())
                continue
        except Exception:
            continue

    # stable unique
    uniq: list[dt.date] = []
    seen: set[dt.date] = set()
    for d in out:
        if d not in seen:
            uniq.append(d)
            seen.add(d)
    return uniq


# ============================================================
# business-day helpers
# ============================================================

def is_business_day(day: dt.date | None = None) -> bool:
    """
    簡易版:
      - 土日以外を営業日扱い
    注:
      - 祝日判定まで厳密にやる場合は別 holiday source に差し替え可能
    """
    day = day or today_date()
    try:
        return day.weekday() < 5
    except Exception:
        return False


def is_today_business_day() -> bool:
    return is_business_day(today_date())


def get_previous_business_day(day: dt.date | None = None) -> dt.date:
    day = day or today_date()
    cur = day - dt.timedelta(days=1)

    # 最大10日さかのぼれば週末は十分に超えられる
    for _ in range(10):
        if is_business_day(cur):
            return cur
        cur -= dt.timedelta(days=1)

    return day - dt.timedelta(days=1)


def get_closed_day_allowed_dates(
    day: dt.date | None = None,
    *,
    include_previous_business_day: bool = True,
) -> list[dt.date]:
    """
    市場休場日でも、当日と前営業日を対象候補にできるようにする。
    """
    day = day or today_date()
    out = [day]

    if include_previous_business_day:
        prev = get_previous_business_day(day)
        if prev not in out:
            out.append(prev)

    return out


def target_dates(
    *,
    business_day: bool | None = None,
    include_previous_business_day: bool = True,
    base_date: dt.date | None = None,
) -> list[dt.date]:
    """
    summary/recovery/bootstrap から使いやすい対象日 helper
    """
    base_date = base_date or today_date()

    if business_day is None:
        business_day = is_business_day(base_date)

    if business_day:
        out = [base_date]
        if include_previous_business_day:
            prev = get_previous_business_day(base_date)
            if prev not in out:
                out.append(prev)
        return out

    return get_closed_day_allowed_dates(
        base_date,
        include_previous_business_day=include_previous_business_day,
    )


# ============================================================
# market session helpers
# ============================================================

def is_preopen_time(now: Optional[dt.datetime] = None) -> bool:
    now = now or now_dt()
    t = now.time()
    return _PREOPEN_START <= t < _MARKET_OPEN


def is_lunch_break_time(now: Optional[dt.datetime] = None) -> bool:
    now = now or now_dt()
    t = now.time()
    return _LUNCH_START <= t < _LUNCH_END


def is_market_session_time(now: Optional[dt.datetime] = None) -> bool:
    now = now or now_dt()
    t = now.time()

    in_morning = _MARKET_OPEN <= t < _LUNCH_START
    in_afternoon = _LUNCH_END <= t <= _MARKET_CLOSE
    return in_morning or in_afternoon


def is_after_market_close(now: Optional[dt.datetime] = None) -> bool:
    now = now or now_dt()
    return now.time() > _MARKET_CLOSE


# ============================================================
# minute-anchor / scheduler helpers
# ============================================================

def minute_anchor_ok(interval: int, now: Optional[dt.datetime] = None) -> bool:
    """
    毎時0分を起点とした minute anchor 判定
    例:
      interval=3 -> 00,03,06,...,57
      interval=5 -> 00,05,10,...,55
    """
    now = now or now_dt()
    try:
        interval = int(interval)
        if interval <= 0:
            return False
        return (now.minute % interval) == 0
    except Exception:
        return False


def should_run_interval(interval: int, now: Optional[dt.datetime] = None) -> bool:
    """
    scheduler から使う公開判定。
    minute_anchor_ok の薄い wrapper。
    """
    return minute_anchor_ok(interval=interval, now=now)


def should_run_1m(now: Optional[dt.datetime] = None) -> bool:
    _ = now or now_dt()
    return True


def should_run_3m(now: Optional[dt.datetime] = None) -> bool:
    return should_run_interval(3, now=now)


def should_run_5m(now: Optional[dt.datetime] = None) -> bool:
    return should_run_interval(5, now=now)


# ============================================================
# misc date helpers for data filters
# ============================================================

def extract_dates_from_datetime_like(values: Iterable[object] | object | None) -> list[dt.date]:
    return normalize_dates(values)


def extract_actual_dates_from_df(df, datetime_col: str = "datetime") -> list[dt.date]:
    """
    DataFrame-like から実際に存在する date を抽出
    pandas 非依存で軽く扱えるよう broad に実装
    """
    try:
        import pandas as pd  # local import

        if df is None or not isinstance(df, pd.DataFrame) or df.empty:
            return []

        if datetime_col not in df.columns:
            return []

        s = pd.to_datetime(df[datetime_col], errors="coerce").dropna()
        if s.empty:
            return []

        dates = [x.date() for x in s.tolist()]
        return normalize_dates(dates)
    except Exception:
        return []


def drop_rows_outside_allowed_dates(df, allowed_dates: Iterable[object] | object | None, datetime_col: str = "datetime"):
    try:
        import pandas as pd  # local import

        if df is None or not isinstance(df, pd.DataFrame) or df.empty:
            return df

        allowed = set(normalize_dates(allowed_dates))
        if not allowed:
            return df

        if datetime_col not in df.columns:
            return df

        x = df.copy()
        s = pd.to_datetime(x[datetime_col], errors="coerce")
        mask = s.notna() & s.dt.date.isin(allowed)
        return x.loc[mask].copy().reset_index(drop=True)
    except Exception:
        return df


def drop_rows_to_explicit_dates(df, explicit_dates: Iterable[object] | object | None, datetime_col: str = "datetime"):
    return drop_rows_outside_allowed_dates(df, explicit_dates, datetime_col=datetime_col)


def filter_latest_per_symbol(df, datetime_col: str = "datetime", symbol_col: str = "symbol"):
    try:
        import pandas as pd  # local import

        if df is None or not isinstance(df, pd.DataFrame) or df.empty:
            return df

        if symbol_col not in df.columns or datetime_col not in df.columns:
            return df

        x = df.copy()
        x[datetime_col] = pd.to_datetime(x[datetime_col], errors="coerce")
        x = x[x[datetime_col].notna()].copy()
        if x.empty:
            return x

        x = x.sort_values([symbol_col, datetime_col], kind="stable")
        x = x.groupby(symbol_col, as_index=False).tail(1)
        return x.reset_index(drop=True)
    except Exception:
        return df


def ensure_dataframe(obj):
    try:
        import pandas as pd  # local import

        if isinstance(obj, pd.DataFrame):
            return obj.copy().reset_index(drop=True)
        if isinstance(obj, pd.Series):
            return pd.DataFrame([obj.to_dict()])
        if isinstance(obj, dict):
            return pd.DataFrame([obj])
        return pd.DataFrame()
    except Exception:
        return obj


def safe_get_series(df, col: str):
    try:
        import pandas as pd  # local import

        if df is None or not isinstance(df, pd.DataFrame) or df.empty:
            return pd.Series(dtype="object")
        if col not in df.columns:
            return pd.Series(dtype="object")
        return df[col]
    except Exception:
        try:
            import pandas as pd  # type: ignore
            return pd.Series(dtype="object")
        except Exception:
            return None


def coerce_datetime_series(series):
    try:
        import pandas as pd  # local import
        return pd.to_datetime(series, errors="coerce")
    except Exception:
        return series


def normalize_datetime_columns(df, columns: Iterable[str] = ("datetime", "end_time", "time", "start_time", "snapshot_time")):
    try:
        import pandas as pd  # local import

        if df is None or not isinstance(df, pd.DataFrame) or df.empty:
            return df

        x = df.copy()
        for c in columns:
            if c in x.columns:
                s = pd.to_datetime(x[c], errors="coerce")
                try:
                    s = s.dt.tz_localize(None)
                except Exception:
                    pass
                x[c] = s
        return x
    except Exception:
        return df


__all__ = [
    # basic datetime
    "now_dt",
    "today_date",
    "minute_of",
    "hour_of",
    "normalize_dates",

    # business day
    "is_business_day",
    "is_today_business_day",
    "get_previous_business_day",
    "get_closed_day_allowed_dates",
    "target_dates",

    # market time
    "is_preopen_time",
    "is_lunch_break_time",
    "is_market_session_time",
    "is_after_market_close",

    # scheduler / minute anchor
    "minute_anchor_ok",
    "should_run_interval",
    "should_run_1m",
    "should_run_3m",
    "should_run_5m",

    # df/date helpers
    "extract_dates_from_datetime_like",
    "extract_actual_dates_from_df",
    "drop_rows_outside_allowed_dates",
    "drop_rows_to_explicit_dates",
    "filter_latest_per_symbol",
    "ensure_dataframe",
    "safe_get_series",
    "coerce_datetime_series",
    "normalize_datetime_columns",
]