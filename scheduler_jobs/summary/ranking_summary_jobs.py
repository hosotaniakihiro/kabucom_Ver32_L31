# ============================================================
# File   : scheduler_jobs/summary/ranking_summary_jobs.py
# Ver    : PRODUCTION-STABLE-REV1.0-RANKING-SUMMARY-JOBS
# ------------------------------------------------------------
# 【概要】
#   ランキング由来サマリー専用の定時ジョブ入口
#
# 【重要方針】
#   - PUSH由来 summary とは完全分離
#   - stock_summary_* は読まない・書かない
#   - ranking_snapshot_1min.current_price を close として扱う
#   - Yahoo 1分足 close はランキング価格系列補完として利用
#   - ranking_summary_1min / 3min / 5min に保存
#
# 【スケジューラ接続】
#   - job_ranking_summary_1m()
#   - job_ranking_summary_3m()
#   - job_ranking_summary_5m()
#   - job_ranking_summary_all()
#
# 【期待ログ】
#   - [RANKING SUMMARY JOB] start interval=1
#   - [RANKING SUMMARY RUNNER] start interval=1
#   - [RANKING TECH] indicators added ...
#   - [RANKING SUMMARY SAVE] saved table=ranking_summary_1min ...
#   - 📊 RANKING SUMMARY TOP10
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import threading
import time
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# Global guards
# ============================================================

_LOCKS: dict[int, threading.Lock] = {
    1: threading.Lock(),
    3: threading.Lock(),
    5: threading.Lock(),
}

_LAST_RUN_AT: dict[int, float] = {
    1: 0.0,
    3: 0.0,
    5: 0.0,
}

_MIN_INTERVAL_SEC: dict[int, float] = {
    1: 20.0,
    3: 40.0,
    5: 40.0,
}


# ============================================================
# Optional global_data sync
# ============================================================

def _get_global_data():
    try:
        from global_state import global_data  # type: ignore
        return global_data
    except Exception:
        pass

    try:
        from core.global_context.context import global_data  # type: ignore
        return global_data
    except Exception:
        return None


def _set_global_attr(name: str, value) -> None:
    gd = _get_global_data()
    if gd is None:
        return

    try:
        setattr(gd, name, value)
    except Exception:
        pass


def _get_global_attr(name: str, default=None):
    gd = _get_global_data()
    if gd is None:
        return default

    try:
        return getattr(gd, name, default)
    except Exception:
        return default


def _store_ranking_summary_cache(interval: int, df: pd.DataFrame) -> None:
    """
    global_data にランキング由来サマリーを保存する。
    PUSH由来 summary とは別名で保持する。
    """
    if df is None:
        return

    cache_name = f"ranking_summary_{interval}min_df"
    _set_global_attr(cache_name, df)

    # 汎用dictも用意
    cache = _get_global_attr("ranking_summary_cache", None)
    if cache is None or not isinstance(cache, dict):
        cache = {}

    cache[int(interval)] = df
    _set_global_attr("ranking_summary_cache", cache)

    _set_global_attr("last_ranking_summary_updated_at", dt.datetime.now())


# ============================================================
# Time helpers
# ============================================================

def _now() -> dt.datetime:
    return dt.datetime.now()


def _is_market_related_time(now: Optional[dt.datetime] = None) -> bool:
    """
    厳密な市場時間判定ではなく、ランキングサマリー実行許可の広め判定。

    理由:
      - 時間外でも最新計算済み結果を表示したい
      - 引け直後はYahoo補完で計算が変わる
      - 起動確認・DB確認のため、完全停止しない方が安全
    """
    now = now or _now()
    t = now.time()

    start = dt.time(8, 30)
    end = dt.time(16, 30)

    return start <= t <= end


def _should_run_by_clock(interval: int, now: Optional[dt.datetime] = None) -> bool:
    """
    毎時00分起点の interval 分判定。

    1min:
      毎分

    3min:
      :00, :03, :06, ... :57

    5min:
      :00, :05, :10, ... :55
    """
    now = now or _now()
    interval = int(interval)

    if interval <= 1:
        return True

    return now.minute % interval == 0


def _guard_duplicate_run(interval: int) -> bool:
    """
    スケジューラ多重発火対策。
    True なら実行OK。
    """
    interval = int(interval)
    now_ts = time.time()
    last = _LAST_RUN_AT.get(interval, 0.0)
    min_sec = _MIN_INTERVAL_SEC.get(interval, 20.0)

    if now_ts - last < min_sec:
        logger.info(
            "[RANKING SUMMARY JOB] skip duplicate interval=%s elapsed=%.1fs min=%.1fs",
            interval,
            now_ts - last,
            min_sec,
        )
        return False

    _LAST_RUN_AT[interval] = now_ts
    return True


# ============================================================
# Core execution
# ============================================================

def _run_interval(
    *,
    interval: int,
    force: bool = False,
    trade_date=None,
    lookback_minutes: int = 240,
    use_yahoo_fill: bool = True,
    persist: bool = True,
    display: bool = True,
    top_n: int = 10,
) -> pd.DataFrame:
    """
    指定intervalのランキング由来サマリーを実行。
    """
    interval = int(interval)
    if interval not in (1, 3, 5):
        raise ValueError(f"unsupported interval: {interval}")

    now = _now()

    if not force:
        if not _should_run_by_clock(interval, now):
            logger.info(
                "[RANKING SUMMARY JOB] skip by clock interval=%s now=%s",
                interval,
                now.strftime("%H:%M:%S"),
            )
            return pd.DataFrame()

        if not _guard_duplicate_run(interval):
            return pd.DataFrame()

    lock = _LOCKS[interval]

    if not lock.acquire(blocking=False):
        logger.warning(
            "[RANKING SUMMARY JOB] skip locked interval=%s",
            interval,
        )
        return pd.DataFrame()

    try:
        logger.info(
            "[RANKING SUMMARY JOB] start interval=%s force=%s "
            "lookback=%s yahoo_fill=%s persist=%s display=%s",
            interval,
            force,
            lookback_minutes,
            use_yahoo_fill,
            persist,
            display,
        )

        _set_global_attr("ranking_summary_running", True)
        _set_global_attr(f"ranking_summary_{interval}min_running", True)

        from trading.ranking.summary.runner import run_ranking_summary_once

        df = run_ranking_summary_once(
            trade_date=trade_date,
            interval=interval,
            lookback_minutes=lookback_minutes,
            use_yahoo_fill=use_yahoo_fill,
            persist=persist,
            display=display,
            top_n=top_n,
        )

        if df is not None and not df.empty:
            _store_ranking_summary_cache(interval, df)

            logger.info(
                "[RANKING SUMMARY JOB] done interval=%s rows=%s symbols=%s "
                "rsi_non_null=%s macd_non_null=%s",
                interval,
                len(df),
                df["symbol"].nunique() if "symbol" in df.columns else 0,
                int(df["rsi"].notna().sum()) if "rsi" in df.columns else 0,
                int(df["macd"].notna().sum()) if "macd" in df.columns else 0,
            )
        else:
            logger.warning(
                "[RANKING SUMMARY JOB] done empty interval=%s",
                interval,
            )

        return df

    except Exception:
        logger.exception(
            "[RANKING SUMMARY JOB] failed interval=%s",
            interval,
        )
        return pd.DataFrame()

    finally:
        _set_global_attr(f"ranking_summary_{interval}min_running", False)
        _set_global_attr("ranking_summary_running", False)
        try:
            lock.release()
        except Exception:
            pass


# ============================================================
# Public jobs
# ============================================================

def job_ranking_summary_1m(
    *,
    force: bool = False,
    trade_date=None,
    lookback_minutes: int = 240,
    use_yahoo_fill: bool = True,
    persist: bool = True,
    display: bool = True,
    top_n: int = 10,
) -> pd.DataFrame:
    """
    1分足ランキング由来サマリー定時ジョブ。
    """
    return _run_interval(
        interval=1,
        force=force,
        trade_date=trade_date,
        lookback_minutes=lookback_minutes,
        use_yahoo_fill=use_yahoo_fill,
        persist=persist,
        display=display,
        top_n=top_n,
    )


def job_ranking_summary_3m(
    *,
    force: bool = False,
    trade_date=None,
    lookback_minutes: int = 240,
    use_yahoo_fill: bool = True,
    persist: bool = True,
    display: bool = True,
    top_n: int = 10,
) -> pd.DataFrame:
    """
    3分足ランキング由来サマリー定時ジョブ。
    """
    return _run_interval(
        interval=3,
        force=force,
        trade_date=trade_date,
        lookback_minutes=lookback_minutes,
        use_yahoo_fill=use_yahoo_fill,
        persist=persist,
        display=display,
        top_n=top_n,
    )


def job_ranking_summary_5m(
    *,
    force: bool = False,
    trade_date=None,
    lookback_minutes: int = 240,
    use_yahoo_fill: bool = True,
    persist: bool = True,
    display: bool = True,
    top_n: int = 10,
) -> pd.DataFrame:
    """
    5分足ランキング由来サマリー定時ジョブ。
    """
    return _run_interval(
        interval=5,
        force=force,
        trade_date=trade_date,
        lookback_minutes=lookback_minutes,
        use_yahoo_fill=use_yahoo_fill,
        persist=persist,
        display=display,
        top_n=top_n,
    )


def job_ranking_summary_all(
    *,
    force: bool = False,
    trade_date=None,
    lookback_minutes: int = 240,
    use_yahoo_fill: bool = True,
    persist: bool = True,
    display: bool = True,
    top_n: int = 10,
) -> dict[int, pd.DataFrame]:
    """
    現在時刻に応じて 1min / 3min / 5min を実行する。

    毎分:
      1min は実行

    :00, :03, :06...
      3min も実行

    :00, :05, :10...
      5min も実行
    """
    now = _now()
    results: dict[int, pd.DataFrame] = {}

    # 1minは毎回
    results[1] = job_ranking_summary_1m(
        force=force,
        trade_date=trade_date,
        lookback_minutes=lookback_minutes,
        use_yahoo_fill=use_yahoo_fill,
        persist=persist,
        display=display,
        top_n=top_n,
    )

    # 3min
    if force or now.minute % 3 == 0:
        results[3] = job_ranking_summary_3m(
            force=force,
            trade_date=trade_date,
            lookback_minutes=lookback_minutes,
            use_yahoo_fill=use_yahoo_fill,
            persist=persist,
            display=display,
            top_n=top_n,
        )
    else:
        results[3] = pd.DataFrame()

    # 5min
    if force or now.minute % 5 == 0:
        results[5] = job_ranking_summary_5m(
            force=force,
            trade_date=trade_date,
            lookback_minutes=lookback_minutes,
            use_yahoo_fill=use_yahoo_fill,
            persist=persist,
            display=display,
            top_n=top_n,
        )
    else:
        results[5] = pd.DataFrame()

    return results


# ============================================================
# Compatibility aliases
# ============================================================

def run_ranking_summary_job(**kwargs):
    return job_ranking_summary_all(**kwargs)


def run_ranking_summary_1min_job(**kwargs):
    return job_ranking_summary_1m(**kwargs)


def run_ranking_summary_3min_job(**kwargs):
    return job_ranking_summary_3m(**kwargs)


def run_ranking_summary_5min_job(**kwargs):
    return job_ranking_summary_5m(**kwargs)


def job_summary_ranking(**kwargs):
    return job_ranking_summary_all(**kwargs)


__all__ = [
    "job_ranking_summary_1m",
    "job_ranking_summary_3m",
    "job_ranking_summary_5m",
    "job_ranking_summary_all",
    "run_ranking_summary_job",
    "run_ranking_summary_1min_job",
    "run_ranking_summary_3min_job",
    "run_ranking_summary_5min_job",
    "job_summary_ranking",
]