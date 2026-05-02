# ============================================================
# File   : scheduler_jobs/summary_jobs.py
# Version: Ver31_L25-SUMMARY-JOBS-COMPAT-SOURCE-SEPARATED
#          -PUSH-RANKING-SPLIT
#          -THIN-COMPAT-LAYER
# ------------------------------------------------------------
# ✔ 旧 import 経路との互換を維持
# ✔ 実体は scheduler_jobs.summary 配下へ委譲
# ✔ job_summary は PUSH専用入口へ接続
# ✔ job_ranking_summary は RANKING専用入口へ接続
# ✔ display も PUSH / RANKING を別公開
# ✔ 互換レイヤは薄く保つ
# ✔ ranking 側未配置でも push 側が死なない
# ============================================================

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


# ============================================================
# core imports (package public exports)
# ============================================================
from scheduler_jobs.summary import (
    # --------------------------------------------------------
    # scheduler / time compat
    # --------------------------------------------------------
    job_summary_scheduler,
    should_run_1m,
    should_run_3m,
    should_run_5m,
    register_summary_tasks,
    register_time_locked_summary_tasks,
    run_summary_tick_once,

    # --------------------------------------------------------
    # time utils
    # --------------------------------------------------------
    now_naive,
    now_dt,
    today_date,
    is_lunch_break,
    floor_to_interval,
    is_time_locked_target,
    resolve_target_intervals,

    # --------------------------------------------------------
    # dataframe / display prepare
    # --------------------------------------------------------
    ensure_df,
    normalize_df,
    extract_latest_timestamp,
    latest_dt_str,
    symbols_count,
    get_primary_dt_col,
    clamp_future_rows,
    select_latest_slot_rows,
    prepare_display_df,

    # --------------------------------------------------------
    # quality guards
    # --------------------------------------------------------
    numeric_sum_abs,
    looks_uncomputed_push_df,
    looks_uncomputed_ranking_df,

    # --------------------------------------------------------
    # fallback loader
    # --------------------------------------------------------
    safe_getattr,
    select_best_candidate,
    today_summary_db_path,
    summary_table_name,
    load_latest_summary_from_db,
    filter_push_like_rows,
    fallback_push_summary_df,
    fallback_ranking_summary_df,

    # --------------------------------------------------------
    # cache
    # --------------------------------------------------------
    save_merged_summary,

    # --------------------------------------------------------
    # push display / jobs
    # --------------------------------------------------------
    display_push_summary,
    print_push_summary,
    print_push_summary_top10,
    job_1m,
    job_3m,
    job_5m,
    job_summary,
    run_push_summary_job,
    run_push_summary_job_compat,

    # --------------------------------------------------------
    # ranking display / jobs
    # --------------------------------------------------------
    display_ranking_summary,
    print_ranking_summary,
    print_ranking_summary_top10,
    job_ranking_1m,
    job_ranking_3m,
    job_ranking_5m,
    job_ranking_summary,
    run_ranking_summary_job,

    # --------------------------------------------------------
    # shared runner facade
    # --------------------------------------------------------
    run_time_locked_summary_jobs,
)

logger.info(
    "[summary_jobs compat] loaded "
    "job_summary=PUSH "
    "job_ranking_summary=RANKING "
    "display_push_summary=PUSH "
    "display_ranking_summary=RANKING"
)


# ============================================================
# optional dependency compat aliases
# ============================================================

def _missing(name: str):
    def _fn(*args, **kwargs):
        raise NotImplementedError(
            f"{name} is not available in the current source-separated summary package"
        )
    _fn.__name__ = name
    return _fn


def _identity_df(df, *args, **kwargs):
    return df


# ============================================================
# calendar compat aliases
# ============================================================

def minute_of(x) -> int:
    try:
        return int(getattr(x, "minute"))
    except Exception:
        return 0


def hour_of(x) -> int:
    try:
        return int(getattr(x, "hour"))
    except Exception:
        return 0


def minute_anchor_ok(interval: int, now=None) -> bool:
    import datetime as dt

    now = now or dt.datetime.now().replace(second=0, microsecond=0)
    try:
        interval = int(interval)
        if interval <= 0:
            return False
        return (now.minute % interval) == 0
    except Exception:
        return False


def should_run_interval(interval: int, now=None) -> bool:
    return minute_anchor_ok(interval, now=now)


def is_lunch_break_time(now=None) -> bool:
    return is_lunch_break(now)


# ============================================================
# optional not-yet-separated compat stubs
# ============================================================

def get_previous_business_day(*args, **kwargs):
    return _missing("get_previous_business_day")(*args, **kwargs)


def is_business_day(*args, **kwargs):
    return _missing("is_business_day")(*args, **kwargs)


def is_today_business_day(*args, **kwargs):
    return _missing("is_today_business_day")(*args, **kwargs)


def get_closed_day_allowed_dates(*args, **kwargs):
    return _missing("get_closed_day_allowed_dates")(*args, **kwargs)


def is_market_session_time(*args, **kwargs):
    return _missing("is_market_session_time")(*args, **kwargs)


def is_preopen_time(*args, **kwargs):
    return _missing("is_preopen_time")(*args, **kwargs)


def is_after_market_close(*args, **kwargs):
    return _missing("is_after_market_close")(*args, **kwargs)


def normalize_dates(*args, **kwargs):
    return _missing("normalize_dates")(*args, **kwargs)


def target_dates(*args, **kwargs):
    return _missing("target_dates")(*args, **kwargs)


# ============================================================
# guard compat aliases
# ============================================================

def ensure_dataframe(obj: Any):
    return ensure_df(obj)


def safe_get_series(*args, **kwargs):
    return _missing("safe_get_series")(*args, **kwargs)


def coerce_datetime_series(*args, **kwargs):
    return _missing("coerce_datetime_series")(*args, **kwargs)


def normalize_datetime_columns(*args, **kwargs):
    return _missing("normalize_datetime_columns")(*args, **kwargs)


def extract_actual_dates_from_df(*args, **kwargs):
    return _missing("extract_actual_dates_from_df")(*args, **kwargs)


def extract_dates_from_datetime_like(*args, **kwargs):
    return _missing("extract_dates_from_datetime_like")(*args, **kwargs)


def drop_rows_outside_allowed_dates(df, *args, **kwargs):
    return _identity_df(df, *args, **kwargs)


def drop_rows_to_explicit_dates(df, *args, **kwargs):
    return _identity_df(df, *args, **kwargs)


def filter_latest_per_symbol(*args, **kwargs):
    return _missing("filter_latest_per_symbol")(*args, **kwargs)


# ============================================================
# dependency compat aliases
# ============================================================

try:
    from trading.summary.push.dependencies import (
        resolve_push_summary_runner,
        resolve_push_display as resolve_display_push_summary,
    )
except Exception:
    def resolve_push_summary_runner(*args, **kwargs):  # type: ignore
        return _missing("resolve_push_summary_runner")(*args, **kwargs)

    def resolve_display_push_summary(*args, **kwargs):  # type: ignore
        return _missing("resolve_display_push_summary")(*args, **kwargs)


try:
    from scheduler_jobs.summary.dependencies import (
        resolve_ranking_summary_runner,
        resolve_display_functions,
    )
except Exception:
    def resolve_ranking_summary_runner(*args, **kwargs):  # type: ignore
        return _missing("resolve_ranking_summary_runner")(*args, **kwargs)

    def resolve_display_functions(*args, **kwargs):  # type: ignore
        return _missing("resolve_display_functions")(*args, **kwargs)


def resolve_display_ranking_summary():
    try:
        pair = resolve_display_functions()
        if isinstance(pair, tuple) and len(pair) >= 2:
            return pair[1]
    except Exception:
        pass
    return display_ranking_summary


def resolve_bootstrap_incremental_rebuild(*args, **kwargs):
    return _missing("resolve_bootstrap_incremental_rebuild")(*args, **kwargs)


def resolve_process_incremental_1m(*args, **kwargs):
    return _missing("resolve_process_incremental_1m")(*args, **kwargs)


def resolve_process_incremental_higher_tf(*args, **kwargs):
    return _missing("resolve_process_incremental_higher_tf")(*args, **kwargs)


# ============================================================
# public exports
# ============================================================
__all__ = [
    # scheduler
    "job_summary_scheduler",
    "should_run_1m",
    "should_run_3m",
    "should_run_5m",
    "register_summary_tasks",
    "register_time_locked_summary_tasks",
    "run_summary_tick_once",

    # runners / jobs
    "job_1m",
    "job_3m",
    "job_5m",
    "job_summary",
    "run_push_summary_job",
    "run_push_summary_job_compat",
    "job_ranking_1m",
    "job_ranking_3m",
    "job_ranking_5m",
    "job_ranking_summary",
    "run_ranking_summary_job",
    "run_time_locked_summary_jobs",

    # display
    "display_push_summary",
    "print_push_summary",
    "print_push_summary_top10",
    "display_ranking_summary",
    "print_ranking_summary",
    "print_ranking_summary_top10",

    # time / calendar
    "now_naive",
    "now_dt",
    "today_date",
    "minute_of",
    "hour_of",
    "floor_to_interval",
    "minute_anchor_ok",
    "should_run_interval",
    "is_time_locked_target",
    "resolve_target_intervals",
    "is_lunch_break",
    "is_lunch_break_time",
    "get_previous_business_day",
    "is_business_day",
    "is_today_business_day",
    "get_closed_day_allowed_dates",
    "is_market_session_time",
    "is_preopen_time",
    "is_after_market_close",
    "normalize_dates",
    "target_dates",

    # guards / df helpers
    "ensure_df",
    "ensure_dataframe",
    "normalize_df",
    "extract_latest_timestamp",
    "latest_dt_str",
    "symbols_count",
    "get_primary_dt_col",
    "clamp_future_rows",
    "select_latest_slot_rows",
    "prepare_display_df",
    "numeric_sum_abs",
    "looks_uncomputed_push_df",
    "looks_uncomputed_ranking_df",
    "safe_get_series",
    "coerce_datetime_series",
    "normalize_datetime_columns",
    "extract_actual_dates_from_df",
    "extract_dates_from_datetime_like",
    "drop_rows_outside_allowed_dates",
    "drop_rows_to_explicit_dates",
    "filter_latest_per_symbol",

    # fallback / cache
    "safe_getattr",
    "select_best_candidate",
    "today_summary_db_path",
    "summary_table_name",
    "load_latest_summary_from_db",
    "filter_push_like_rows",
    "fallback_push_summary_df",
    "fallback_ranking_summary_df",
    "save_merged_summary",

    # dependencies
    "resolve_push_summary_runner",
    "resolve_ranking_summary_runner",
    "resolve_display_push_summary",
    "resolve_display_ranking_summary",
    "resolve_display_functions",
    "resolve_bootstrap_incremental_rebuild",
    "resolve_process_incremental_1m",
    "resolve_process_incremental_higher_tf",
]