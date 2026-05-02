# ============================================================
# File   : scheduler_jobs/summary/runners.py
# Version: PRODUCTION-STABLE-SUMMARY-RUNNERS-COMPAT-SHIM-V12.1
# ------------------------------------------------------------
# 【概要】
#   定時サマリー runner の互換入口。
#
# 【旧】
#   scheduler_jobs.summary.runners に全実装を集約
#
# 【新】
#   scheduler_jobs.summary.runner_core
#   scheduler_jobs.summary.time_locked_runner
#   scheduler_jobs.summary.output_normalizer
#   scheduler_jobs.summary.safe_io
#   scheduler_jobs.summary.closed_market_display
#   scheduler_jobs.summary.summary_ai_entry_hook
#   scheduler_jobs.summary.runner_utils
#
# 【重要】
#   - 既存 import を壊さない
#   - 実処理は分割済みモジュールへ委譲
# ============================================================

from __future__ import annotations

from .runner_core import (
    job_1m,
    job_3m,
    job_5m,
    job_summary,
    job_ranking_1m,
    job_ranking_3m,
    job_ranking_5m,
    job_ranking_summary,
    run_push_summary_job,
    run_ranking_summary_job,
)

from .time_locked_runner import (
    run_time_locked_summary_jobs,
)

from .output_normalizer import (
    normalize_runner_output,
    log_job_result,
)

__all__ = [
    "job_1m",
    "job_3m",
    "job_5m",
    "job_summary",
    "job_ranking_1m",
    "job_ranking_3m",
    "job_ranking_5m",
    "job_ranking_summary",
    "run_time_locked_summary_jobs",
    "run_push_summary_job",
    "run_ranking_summary_job",
    "normalize_runner_output",
    "log_job_result",
]