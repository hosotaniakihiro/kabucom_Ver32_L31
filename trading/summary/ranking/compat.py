# ============================================================
# File   : trading/summary/ranking/compat.py
# Ver    : PRODUCTION-STABLE-RANKING-COMPAT-V1.0
# ------------------------------------------------------------
# ✔ 既存コードから RANKING系へ寄せるための互換レイヤ
# ✔ PUSH系は一切含まない
# ✔ 古い命名の薄い alias を提供
# ✔ ranking 本体は trading.ranking.ranking_summary_engine を利用
# ============================================================

from __future__ import annotations

from trading.ranking.ranking_summary_engine import (
    build_ranking_summary,
    run_ranking_summary,
    run_ranking_summary_job as core_run_ranking_summary_job,
    job_ranking_summary as core_job_ranking_summary,
    ranking_summary_engine,
)

from .display import (
    display_ranking_summary,
    print_ranking_summary_top10,
)
from .runner import (
    job_ranking_1m,
    job_ranking_3m,
    job_ranking_5m,
    job_ranking_summary,
    run_ranking_summary_job,
    run_time_locked_jobs,
)

__all__ = [
    # engine aliases
    "build_ranking_summary",
    "run_ranking_summary",
    "core_run_ranking_summary_job",
    "core_job_ranking_summary",
    "ranking_summary_engine",

    # runner aliases
    "job_ranking_1m",
    "job_ranking_3m",
    "job_ranking_5m",
    "job_ranking_summary",
    "run_ranking_summary_job",
    "run_time_locked_jobs",

    # display aliases
    "display_ranking_summary",
    "print_ranking_summary_top10",
]