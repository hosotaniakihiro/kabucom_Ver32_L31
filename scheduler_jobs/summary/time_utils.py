# ============================================================
# File   : scheduler_jobs/summary/time_utils.py
# Function:
#   - 定時サマリー系の時刻関連 helper を提供する
#   - stale / future / today 判定を行う
#   - 毎時0分起点の 1分 / 3分 / 5分 の time-locked 判定を行う
#   - now 基準を統一し、tz-aware / naive 混在を吸収する
#   - 表示用アンカー時刻を営業セッション基準で解決する
#   - 昼休み / 引け後 / 翌朝 / 休場日の表示スロットを安定して返す
# ------------------------------------------------------------
# Ver    : PRODUCTION-STABLE-SUMMARY-TIME-UTILS-V4.0
#          -MARKET-SESSION-ANCHOR
#          -LUNCH-CLOSE-HOLIDAY-DISPLAY-FIX
#          -TIMELOCK-SAFE
# ------------------------------------------------------------
# ✔ 時刻関連 helper
# ✔ stale / future / today 判定
# ✔ 毎時0分起点の time-locked 判定
# ✔ now 基準を統一
# ✔ tz-aware / naive 混在に強化
# ✔ 表示用アンカー時刻を営業セッション基準で解決
# ✔ 11:30-12:30 は 11:30 固定表示
# ✔ 15:30-翌9:00 は 15:30 固定表示
# ✔ 休場日は前営業日 15:30 表示
# ✔ 例外耐性
# ============================================================

from __future__ import annotations

import datetime as dt
import os
import sys
from typing import Optional, Tuple

import pandas as pd


# ============================================================
# basic datetime helpers
# ============================================================

def now_naive() -> dt.datetime:
    """
    現在時刻を naive datetime で返す。
    """
    return dt.datetime.now().replace(tzinfo=None, microsecond=0)


def _to_naive_datetime(x) -> Optional[dt.datetime]:
    """
    pd.Timestamp / datetime / 文字列などを naive datetime へ寄せる。
    """
    try:
        if x is None:
            return None

        if isinstance(x, pd.Timestamp):
            try:
                if x.tzinfo is not None:
                    x = x.tz_localize(None)
            except Exception:
                try:
                    x = x.tz_convert(None)
                except Exception:
                    pass
            return x.to_pydatetime().replace(tzinfo=None, microsecond=0)

        if isinstance(x, dt.datetime):
            if x.tzinfo is not None:
                return x.replace(tzinfo=None, microsecond=0)
            return x.replace(microsecond=0)

        y = pd.to_datetime(x, errors="coerce")
        if pd.isna(y):
            return None

        try:
            if getattr(y, "tzinfo", None) is not None:
                y = y.tz_localize(None)
        except Exception:
            try:
                y = y.tz_convert(None)
            except Exception:
                pass

        if isinstance(y, pd.Timestamp):
            return y.to_pydatetime().replace(tzinfo=None, microsecond=0)

    except Exception:
        return None

    return None


def today_date(now: Optional[dt.datetime] = None) -> dt.date:
    base = _to_naive_datetime(now) or now_naive()
    return base.date()


def _combine(d: dt.date, hh: int, mm: int) -> dt.datetime:
    return dt.datetime(d.year, d.month, d.day, hh, mm, 0)


# ============================================================
# business day helpers
# ============================================================

def _load_business_day_predicates():
    """
    既存 project 側の business_day_utils を優先して使う。
    関数名ゆらぎも吸収する。
    """
    try:
        import utils.business_day_utils as bdu  # type: ignore

        def _is_bd(d: dt.date) -> bool:
            for name in (
                "is_business_day",
                "is_trading_day",
                "is_market_open_day",
                "is_open_day",
            ):
                fn = getattr(bdu, name, None)
                if callable(fn):
                    try:
                        return bool(fn(d))
                    except TypeError:
                        try:
                            return bool(fn(target_date=d))
                        except Exception:
                            pass
                    except Exception:
                        pass

            weekday_ok = d.weekday() < 5

            for name in (
                "is_holiday",
                "is_jp_holiday",
                "is_market_holiday",
            ):
                fn = getattr(bdu, name, None)
                if callable(fn):
                    try:
                        return weekday_ok and (not bool(fn(d)))
                    except Exception:
                        pass

            return weekday_ok

        return _is_bd

    except Exception:
        return lambda d: d.weekday() < 5


def is_business_day(d: dt.date) -> bool:
    fn = _load_business_day_predicates()
    try:
        return bool(fn(d))
    except Exception:
        return d.weekday() < 5


def previous_business_day(d: dt.date) -> dt.date:
    cur = d - dt.timedelta(days=1)
    for _ in range(370):
        if is_business_day(cur):
            return cur
        cur -= dt.timedelta(days=1)
    return d - dt.timedelta(days=1)


def next_business_day(d: dt.date) -> dt.date:
    cur = d + dt.timedelta(days=1)
    for _ in range(370):
        if is_business_day(cur):
            return cur
        cur += dt.timedelta(days=1)
    return d + dt.timedelta(days=1)


# ============================================================
# session helpers
# ============================================================

def is_lunch_break(now: Optional[dt.datetime] = None) -> bool:
    now = _to_naive_datetime(now) or now_naive()
    t = now.time()
    return dt.time(11, 30) <= t < dt.time(12, 30)


def is_morning_session(now: Optional[dt.datetime] = None) -> bool:
    now = _to_naive_datetime(now) or now_naive()
    t = now.time()
    return dt.time(9, 0) <= t <= dt.time(11, 30)


def is_afternoon_session(now: Optional[dt.datetime] = None) -> bool:
    now = _to_naive_datetime(now) or now_naive()
    t = now.time()
    return dt.time(12, 30) <= t <= dt.time(15, 30)


def is_market_session(now: Optional[dt.datetime] = None) -> bool:
    now = _to_naive_datetime(now) or now_naive()
    d = now.date()
    return is_business_day(d) and (is_morning_session(now) or is_afternoon_session(now))


# ============================================================
# freshness / stale / future helpers
# ============================================================

def stale_limit_minutes(
    interval: int,
    *,
    for_ranking: bool = False,
    now: Optional[dt.datetime] = None,
) -> int:
    """
    stale 判定の許容分数。
    昼休みや時間外では少し緩める。
    """
    interval = int(interval)
    now = _to_naive_datetime(now) or now_naive()

    if is_lunch_break(now):
        return 70 if for_ranking else 75

    if not is_market_session(now):
        # 時間外は前営業日表示も許容
        return 60 * 18

    if interval <= 1:
        return 7
    if interval <= 3:
        return 12
    if interval <= 5:
        return 18
    return 25


def future_tolerance_seconds(interval: int) -> int:
    try:
        interval = int(interval)
    except Exception:
        interval = 1

    if interval <= 1:
        return 45
    if interval <= 3:
        return 75
    if interval <= 5:
        return 105
    return 150


def is_future_timestamp(
    ts: Optional[pd.Timestamp],
    *,
    interval: int,
    now: Optional[dt.datetime] = None,
) -> bool:
    if ts is None:
        return False

    ts_dt = _to_naive_datetime(ts)
    now_dt = _to_naive_datetime(now) or now_naive()

    if ts_dt is None:
        return False

    tol_sec = future_tolerance_seconds(interval)
    try:
        delta_sec = (ts_dt - now_dt).total_seconds()
        return delta_sec > tol_sec
    except Exception:
        return False


def is_today_timestamp(
    ts: Optional[pd.Timestamp],
    *,
    now: Optional[dt.datetime] = None,
) -> bool:
    if ts is None:
        return False

    ts_dt = _to_naive_datetime(ts)
    if ts_dt is None:
        return False

    try:
        return ts_dt.date() == today_date(now=now)
    except Exception:
        return False


def age_minutes(ts: Optional[pd.Timestamp], now: Optional[dt.datetime] = None) -> Optional[float]:
    if ts is None:
        return None

    ts_dt = _to_naive_datetime(ts)
    now_dt = _to_naive_datetime(now) or now_naive()

    if ts_dt is None:
        return None

    try:
        return (now_dt - ts_dt).total_seconds() / 60.0
    except Exception:
        return None


def is_fresh_timestamp(
    ts: Optional[pd.Timestamp],
    interval: int,
    *,
    for_ranking: bool = False,
    now: Optional[dt.datetime] = None,
) -> bool:
    """
    freshness 判定。
    市場外では「当日でない」だけで stale 扱いしない。
    前営業日 15:30 表示を許容する。
    """
    if ts is None:
        return False

    now_dt = _to_naive_datetime(now) or now_naive()

    if is_future_timestamp(ts, interval=interval, now=now_dt):
        return False

    age_min = age_minutes(ts, now=now_dt)
    if age_min is None:
        return False

    if age_min < 0:
        return True

    ts_dt = _to_naive_datetime(ts)
    if ts_dt is None:
        return False

    # 市場時間中 / 昼休み中は当日データであることを要求
    if is_market_session(now_dt) or is_lunch_break(now_dt):
        if ts_dt.date() != now_dt.date():
            return False

    limit = stale_limit_minutes(interval, for_ranking=for_ranking, now=now_dt)
    return age_min <= limit


# ============================================================
# interval helpers
# ============================================================

def floor_to_interval(now: dt.datetime, interval: int) -> dt.datetime:
    now_dt = _to_naive_datetime(now) or now_naive()

    try:
        interval = max(int(interval), 1)
    except Exception:
        interval = 1

    minute = (now_dt.minute // interval) * interval
    return now_dt.replace(minute=minute, second=0, microsecond=0)


def ceil_to_interval(now: dt.datetime, interval: int) -> dt.datetime:
    now_dt = _to_naive_datetime(now) or now_naive()

    try:
        interval = max(int(interval), 1)
    except Exception:
        interval = 1

    floored = floor_to_interval(now_dt, interval)
    if floored == now_dt.replace(second=0, microsecond=0):
        return floored
    return floored + dt.timedelta(minutes=interval)


def is_time_locked_target(now: dt.datetime, interval: int) -> bool:
    """
    毎時0分起点の time-locked 判定。
    """
    now_dt = _to_naive_datetime(now) or now_naive()

    try:
        interval = int(interval)
        if interval <= 0:
            return False
        return (now_dt.minute % interval) == 0
    except Exception:
        return False


def resolve_target_intervals_core(now: Optional[dt.datetime] = None) -> list[int]:
    """
    定時計算対象 intervals を返す。
    仕様:
      - market session 内だけ計算対象
      - 毎時0分起点
      - 1分 / 3分 / 5分
    """
    now_dt = (_to_naive_datetime(now) or now_naive()).replace(microsecond=0)

    if not is_market_session(now_dt):
        return []

    targets: list[int] = []
    for interval in (1, 3, 5):
        if is_time_locked_target(now_dt, interval):
            targets.append(interval)

    return targets


# --- summary_db_realtime_priority_patch (native化, 元 core/startup/summary_db_realtime_priority_patch.py) ---
# main_database.py/summary_database_runner等のDB/data collectorプロセスでは、1m/3m/5m/ranking
# summaryを同一tickで待つと1m summaryのlatest_dtが5-8分遅れ、main.py側でSUMMARY_AI candidateが
# staleとしてスキップされる問題への対処。DB側プロセスかつ場中は、対象intervalsを1分足のみに
# 絞り3m/5mはmain.py側のMTF fallback/後続処理に任せる。
_SUMMARY_DB_REALTIME_PRIORITY_TRUE = {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
_SUMMARY_DB_REALTIME_PRIORITY_FALSE = {"0", "false", "no", "n", "off", "ng", "disable", "disabled", ""}
_SUMMARY_DB_REALTIME_PRIORITY_ENV_DEFAULTS_APPLIED = False


def _summary_db_realtime_priority_env_bool(name: str, default: bool) -> bool:
    try:
        raw = os.getenv(name)
        if raw is None:
            return bool(default)
        s = str(raw).strip().lower()
        if s in _SUMMARY_DB_REALTIME_PRIORITY_TRUE:
            return True
        if s in _SUMMARY_DB_REALTIME_PRIORITY_FALSE:
            return False
    except Exception:
        pass
    return bool(default)


def _summary_db_realtime_priority_is_db_process() -> bool:
    try:
        argv = " ".join(str(x).replace("\\", "/").lower() for x in sys.argv)
        if any(
            x in argv
            for x in (
                "main_database.py",
                "data_collectors_runner.py",
                "summary_database_runner.py",
                "push_receiver_runner.py",
                "ranking_collector_runner.py",
                "yahoo_complement_runner.py",
                "db_prepare_runner.py",
            )
        ):
            return True
        return any(
            _summary_db_realtime_priority_env_bool(k, False)
            for k in (
                "AUTOSTOCK_MAIN_DATABASE_PROCESS",
                "AUTOSTOCK_DATA_COLLECTORS_PROCESS",
                "AUTOSTOCK_SUMMARY_DB_WRITER",
                "AUTOSTOCK_RANKING_COLLECTOR_PROCESS",
            )
        )
    except Exception:
        return False


def _summary_db_realtime_priority_enabled() -> bool:
    if str(os.environ.get("DISABLE_SUMMARY_DB_REALTIME_PRIORITY_PATCH", "")).strip() == "1":
        return False
    return (
        _summary_db_realtime_priority_env_bool("SUMMARY_DB_REALTIME_PRIORITY_ENABLED", True)
        and _summary_db_realtime_priority_is_db_process()
    )


def _summary_db_realtime_priority_force_env_defaults() -> None:
    """DB側では1m保存を詰まらせる設定を既定で抑える (プロセス内で1回だけ適用)。"""
    global _SUMMARY_DB_REALTIME_PRIORITY_ENV_DEFAULTS_APPLIED
    if _SUMMARY_DB_REALTIME_PRIORITY_ENV_DEFAULTS_APPLIED:
        return
    defaults = {
        "SUMMARY_PARALLEL_RANKING_ENABLED": "0",
        "SUMMARY_PARALLEL_FORCE_1_3_5": "0",
        "SUMMARY_PUSH_DISPLAY_ALL_INTERVALS": "0",
        "SUMMARY_PUSH_BG_ALL_INTERVALS": "0",
        "SUMMARY_PUSH_BG_LONG_INTERVALS": "0",
        "SUMMARY_PARALLEL_INTERVAL_WORKERS": "1",
        "SUMMARY_PUSH_BG_INTERVAL_WORKERS": "1",
        "SUMMARY_PARALLEL_TIMEOUT_MIN_SEC": "15",
        "SUMMARY_PARALLEL_INTERVAL_TIMEOUT_SEC": "25",
        "SUMMARY_DB_REALTIME_1M_ONLY_IN_SESSION": "1",
    }
    for k, v in defaults.items():
        try:
            old = os.getenv(k)
            if old is None or str(old).strip() == "":
                os.environ[k] = v
        except Exception:
            pass
    _SUMMARY_DB_REALTIME_PRIORITY_ENV_DEFAULTS_APPLIED = True


def resolve_target_intervals(now: Optional[dt.datetime] = None) -> list[int]:
    if _summary_db_realtime_priority_enabled():
        try:
            if _summary_db_realtime_priority_env_bool("SUMMARY_DB_REALTIME_1M_ONLY_IN_SESSION", True) and is_market_session(now):
                return [1]
        except Exception:
            pass
    return resolve_target_intervals_core(now)


# 旧パッチの install() はモジュールimport連鎖の早い段階 (core/startup/runtime_env/defaults_patch.py
# 経由) で1回だけ呼ばれ、DBプロセスならその場でenvデフォルトを強制していた。native化後も
# 同じタイミング (このモジュールが最初にimportされた時点) で1回だけ適用する。
if _summary_db_realtime_priority_enabled():
    _summary_db_realtime_priority_force_env_defaults()


# ============================================================
# display anchor / slot helpers
# ============================================================

def resolve_display_anchor(now: Optional[dt.datetime] = None) -> Tuple[dt.date, dt.datetime]:
    """
    表示の基準アンカーを返す。

    仕様:
    - 09:00-11:30   : 現在時刻
    - 11:30-12:30   : 当日 11:30 固定
    - 12:30-15:30   : 現在時刻
    - 15:30-翌09:00 : 当日 15:30 固定
    - 非営業日      : 前営業日 15:30 固定
    """
    now_dt = _to_naive_datetime(now) or now_naive()
    today = now_dt.date()

    t0900 = _combine(today, 9, 0)
    t1130 = _combine(today, 11, 30)
    t1230 = _combine(today, 12, 30)
    t1530 = _combine(today, 15, 30)

    if not is_business_day(today):
        prev_bd = previous_business_day(today)
        return prev_bd, _combine(prev_bd, 15, 30)

    if now_dt < t0900:
        prev_bd = previous_business_day(today)
        return prev_bd, _combine(prev_bd, 15, 30)

    if t0900 <= now_dt <= t1130:
        return today, now_dt.replace(second=0, microsecond=0)

    if t1130 < now_dt < t1230:
        return today, t1130

    if t1230 <= now_dt <= t1530:
        return today, now_dt.replace(second=0, microsecond=0)

    return today, t1530


def resolve_display_slot(
    interval: int,
    *,
    now: Optional[dt.datetime] = None,
) -> Tuple[dt.date, dt.datetime]:
    """
    表示する足のスロットを返す。
    - 1分: anchorそのまま
    - 3分: anchor以下の3分足へ切り下げ
    - 5分: anchor以下の5分足へ切り下げ
    """
    target_date, anchor_dt = resolve_display_anchor(now=now)

    try:
        interval = max(int(interval), 1)
    except Exception:
        interval = 1

    if interval <= 1:
        return target_date, anchor_dt.replace(second=0, microsecond=0)

    floored = floor_to_interval(anchor_dt, interval)
    return floored.date(), floored


def market_session_bounds(target_date: dt.date) -> tuple[dt.datetime, dt.datetime, dt.datetime, dt.datetime]:
    """
    指定営業日の市場セッション境界を返す。
    """
    return (
        _combine(target_date, 9, 0),
        _combine(target_date, 11, 30),
        _combine(target_date, 12, 30),
        _combine(target_date, 15, 30),
    )


# ============================================================
# exports
# ============================================================

__all__ = [
    "now_naive",
    "today_date",
    "is_business_day",
    "previous_business_day",
    "next_business_day",
    "is_lunch_break",
    "is_morning_session",
    "is_afternoon_session",
    "is_market_session",
    "stale_limit_minutes",
    "future_tolerance_seconds",
    "is_future_timestamp",
    "is_today_timestamp",
    "age_minutes",
    "is_fresh_timestamp",
    "floor_to_interval",
    "ceil_to_interval",
    "is_time_locked_target",
    "resolve_target_intervals",
    "resolve_display_anchor",
    "resolve_display_slot",
    "market_session_bounds",
]