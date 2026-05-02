# ============================================================
# File   : scheduler_jobs/summary/__init__.py
# Ver    : PRODUCTION-STABLE-SUMMARY-PACKAGE-V1.3
#          -SOURCE-SEPARATED-EXPORTS
#          -PUSH-RANKING-SPLIT
#          -DISPLAY-SPLIT-COMPAT
# ------------------------------------------------------------
# ✔ summary package の公開入口
# ✔ PUSH由来 / RANKING由来 を export レベルで分離
# ✔ job_summary は PUSH専用入口へ接続
# ✔ job_ranking_summary は RANKING専用入口へ接続
# ✔ summary_jobs.py 互換で使う主要シンボルを公開
# ✔ ranking 側未配置でも push 側が死なない
# ✔ 未実装シンボルは無理に export しない
# ✔ display.py 7分割後も公開関数名の互換を維持
# ============================================================

from __future__ import annotations

# ============================================================
# time utils
# ============================================================
from .time_utils import (
    now_naive,
    today_date,
    is_lunch_break,
    stale_limit_minutes,
    future_tolerance_seconds,
    is_future_timestamp,
    is_today_timestamp,
    age_minutes,
    is_fresh_timestamp,
    floor_to_interval,
    is_time_locked_target,
    resolve_target_intervals,
)

# ============================================================
# display prepare / dataframe helpers
# ============================================================
from .display_prepare import (
    ensure_df,
    normalize_df,
    extract_latest_timestamp,
    latest_dt_str,
    symbols_count,
    get_primary_dt_col,
    clamp_future_rows,
    select_latest_slot_rows,
    prepare_display_df,
)

# ============================================================
# quality guards
# ============================================================
from .quality_guards import (
    numeric_sum_abs,
    looks_uncomputed_push_df,
    looks_uncomputed_ranking_df,
)

# ============================================================
# fallback loader
# ============================================================
from .fallback_loader import (
    safe_getattr,
    select_best_candidate,
    today_summary_db_path,
    summary_table_name,
    load_latest_summary_from_db,
    filter_push_like_rows,
    fallback_push_summary_df,
    fallback_ranking_summary_df,
)

# ============================================================
# cache writer
# ============================================================
from .cache_writer import (
    save_merged_summary,
)

# ============================================================
# split display exports
#   display.py は split 後の公開入口を維持する
# ============================================================
try:
    from .display import (
        print_summary_top10,
        print_ranking_summary_top10,
        print_push_summary,
        print_ranking_summary,
        display_summary,
        display_push_summary,
        display_ranking_summary,
        display_ai_passed_summary,
    )
except Exception:
    # display split が未配置でも package import で全体を殺さない
    def print_summary_top10(*args, **kwargs):  # type: ignore
        raise RuntimeError("print_summary_top10 is not available")

    def print_ranking_summary_top10(*args, **kwargs):  # type: ignore
        raise RuntimeError("print_ranking_summary_top10 is not available")

    def print_push_summary(*args, **kwargs):  # type: ignore
        raise RuntimeError("print_push_summary is not available")

    def print_ranking_summary(*args, **kwargs):  # type: ignore
        raise RuntimeError("print_ranking_summary is not available")

    def display_summary(*args, **kwargs):  # type: ignore
        raise RuntimeError("display_summary is not available")

    def display_push_summary(*args, **kwargs):  # type: ignore
        raise RuntimeError("display_push_summary is not available")

    def display_ranking_summary(*args, **kwargs):  # type: ignore
        raise RuntimeError("display_ranking_summary is not available")

    def display_ai_passed_summary(*args, **kwargs):  # type: ignore
        raise RuntimeError("display_ai_passed_summary is not available")


# ============================================================
# PUSH display exports
#   新設 PUSH 専用 display を優先
# ============================================================
try:
    from trading.summary.push.display import (
        display_push_summary as _push_display_push_summary,
        print_push_summary_top10 as _push_print_push_summary_top10,
    )

    display_push_summary = _push_display_push_summary  # type: ignore
    print_push_summary_top10 = _push_print_push_summary_top10  # type: ignore

except Exception:
    try:
        from .display import (
            display_push_summary as _local_display_push_summary,
            print_summary_top10 as _local_print_summary_top10,
        )

        display_push_summary = _local_display_push_summary  # type: ignore

        def print_push_summary_top10(*args, **kwargs):  # type: ignore
            return _local_print_summary_top10(*args, **kwargs)

    except Exception:
        def print_push_summary_top10(*args, **kwargs):  # type: ignore
            return display_push_summary(*args, **kwargs)


# ============================================================
# RANKING display exports
#   ranking 側は split 後 display.py に委譲
# ============================================================
try:
    from .display import (
        display_ranking_summary as _local_display_ranking_summary,
        print_ranking_summary_top10 as _local_print_ranking_summary_top10,
    )

    display_ranking_summary = _local_display_ranking_summary  # type: ignore
    print_ranking_summary_top10 = _local_print_ranking_summary_top10  # type: ignore

except Exception:
    def display_ranking_summary(*args, **kwargs):  # type: ignore
        raise RuntimeError("ranking display entrypoint is not available")

    def print_ranking_summary_top10(*args, **kwargs):  # type: ignore
        return display_ranking_summary(*args, **kwargs)


# ============================================================
# PUSH summary exports
#   job_summary は PUSH専用入口
# ============================================================
from .push_summary import (
    job_1m,
    job_3m,
    job_5m,
    job_summary,
    run_push_summary_job,
    run_push_summary_job_compat,
)

# ============================================================
# RANKING summary exports
#   ranking 側は別入口
# ============================================================
try:
    from .ranking_summary import (
        job_ranking_1m,
        job_ranking_3m,
        job_ranking_5m,
        job_ranking_summary,
        run_ranking_summary_job,
    )
except Exception:
    def job_ranking_1m(*args, **kwargs):  # type: ignore
        raise RuntimeError("ranking summary entrypoint is not available")

    def job_ranking_3m(*args, **kwargs):  # type: ignore
        raise RuntimeError("ranking summary entrypoint is not available")

    def job_ranking_5m(*args, **kwargs):  # type: ignore
        raise RuntimeError("ranking summary entrypoint is not available")

    def job_ranking_summary(*args, **kwargs):  # type: ignore
        raise RuntimeError("ranking summary entrypoint is not available")

    def run_ranking_summary_job(*args, **kwargs):  # type: ignore
        raise RuntimeError("ranking summary entrypoint is not available")


# ============================================================
# runners
#   旧 callers 向けに残す
# ============================================================
try:
    from .runners import (
        run_time_locked_summary_jobs,
    )
except Exception:
    def run_time_locked_summary_jobs(*args, **kwargs):  # type: ignore
        raise RuntimeError("run_time_locked_summary_jobs is not available")


# ============================================================
# scheduler
# ============================================================
from .scheduler import (
    register_summary_tasks,
    register_time_locked_summary_tasks,
    run_summary_tick_once,
)

# ============================================================
# optional compat-friendly aliases
# ============================================================

def now_dt():
    return now_naive()


def should_run_1m(now=None) -> bool:
    return True


def should_run_3m(now=None) -> bool:
    import datetime as dt

    now = now or dt.datetime.now().replace(second=0, microsecond=0)
    return bool(is_time_locked_target(now, 3))


def should_run_5m(now=None) -> bool:
    import datetime as dt

    now = now or dt.datetime.now().replace(second=0, microsecond=0)
    return bool(is_time_locked_target(now, 5))


def job_summary_scheduler(*args, **kwargs):
    return run_summary_tick_once(*args, **kwargs)


# ============================================================
# public exports
# ============================================================
__all__ = [
    # scheduler compat
    "job_summary_scheduler",
    "should_run_1m",
    "should_run_3m",
    "should_run_5m",
    "register_summary_tasks",
    "register_time_locked_summary_tasks",
    "run_summary_tick_once",

    # time utils
    "now_naive",
    "now_dt",
    "today_date",
    "is_lunch_break",
    "stale_limit_minutes",
    "future_tolerance_seconds",
    "is_future_timestamp",
    "is_today_timestamp",
    "age_minutes",
    "is_fresh_timestamp",
    "floor_to_interval",
    "is_time_locked_target",
    "resolve_target_intervals",

    # dataframe / display prepare
    "ensure_df",
    "normalize_df",
    "extract_latest_timestamp",
    "latest_dt_str",
    "symbols_count",
    "get_primary_dt_col",
    "clamp_future_rows",
    "select_latest_slot_rows",
    "prepare_display_df",

    # quality guards
    "numeric_sum_abs",
    "looks_uncomputed_push_df",
    "looks_uncomputed_ranking_df",

    # fallback loader
    "safe_getattr",
    "select_best_candidate",
    "today_summary_db_path",
    "summary_table_name",
    "load_latest_summary_from_db",
    "filter_push_like_rows",
    "fallback_push_summary_df",
    "fallback_ranking_summary_df",

    # cache
    "save_merged_summary",

    # split display public entrypoints
    "print_summary_top10",
    "print_ranking_summary_top10",
    "print_push_summary",
    "print_ranking_summary",
    "display_summary",
    "display_push_summary",
    "display_ranking_summary",
    "display_ai_passed_summary",
    "print_push_summary_top10",

    # push runners / jobs
    "job_1m",
    "job_3m",
    "job_5m",
    "job_summary",
    "run_push_summary_job",
    "run_push_summary_job_compat",

    # ranking runners / jobs
    "job_ranking_1m",
    "job_ranking_3m",
    "job_ranking_5m",
    "job_ranking_summary",
    "run_ranking_summary_job",

    # shared runner facade
    "run_time_locked_summary_jobs",
]