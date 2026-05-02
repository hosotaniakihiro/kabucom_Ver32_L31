# ============================================================
# File   : trading/summary/ranking/__init__.py
# Ver    : PRODUCTION-STABLE-RANKING-PACKAGE-V1.0
# ------------------------------------------------------------
# ✔ RANKING系パッケージの公開入口
# ✔ runner / display / fallback / cache / dependencies を再 export
# ✔ PUSH系は一切公開しない
# ✔ ranking 本体は trading.ranking.ranking_summary_engine を利用
# ============================================================

from __future__ import annotations

from .cache_writer import save_ranking_summary
from .dependencies import (
    resolve_ranking_cache_writer,
    resolve_ranking_display,
    resolve_ranking_fallback_loader,
    resolve_ranking_quality_guard,
    resolve_ranking_row_filter,
    resolve_ranking_summary_runner,
)
from .display import (
    display_ranking_summary,
    print_ranking_summary_top10,
)
from .display_prepare import (
    clamp_future_rows,
    latest_dt,
    latest_dt_str,
    normalize_df,
    symbols_count,
)
from .fallback_loader import (
    fallback_ranking_summary_df,
    filter_ranking_like_rows,
)
from .guards import looks_uncomputed_ranking_df
from .runner import (
    job_ranking_1m,
    job_ranking_3m,
    job_ranking_5m,
    job_ranking_summary,
    run_ranking_summary_job,
    run_time_locked_jobs,
)
from .time_utils import (
    floor_to_interval,
    is_afternoon_session,
    is_lunch_break,
    is_market_session,
    is_morning_session,
    is_weekend,
    now_naive,
    resolve_display_slot,
    resolve_target_intervals,
    today_date,
)

__all__ = [
    # cache
    "save_ranking_summary",

    # dependencies
    "resolve_ranking_cache_writer",
    "resolve_ranking_display",
    "resolve_ranking_fallback_loader",
    "resolve_ranking_quality_guard",
    "resolve_ranking_row_filter",
    "resolve_ranking_summary_runner",

    # display
    "display_ranking_summary",
    "print_ranking_summary_top10",

    # display prepare
    "clamp_future_rows",
    "latest_dt",
    "latest_dt_str",
    "normalize_df",
    "symbols_count",

    # fallback
    "fallback_ranking_summary_df",
    "filter_ranking_like_rows",

    # guards
    "looks_uncomputed_ranking_df",

    # runner
    "job_ranking_1m",
    "job_ranking_3m",
    "job_ranking_5m",
    "job_ranking_summary",
    "run_ranking_summary_job",
    "run_time_locked_jobs",

    # time utils
    "floor_to_interval",
    "is_afternoon_session",
    "is_lunch_break",
    "is_market_session",
    "is_morning_session",
    "is_weekend",
    "now_naive",
    "resolve_display_slot",
    "resolve_target_intervals",
    "today_date",
]