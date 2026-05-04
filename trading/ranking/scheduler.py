# ============================================================
# File   : trading/ranking/scheduler.py
# Version: PRODUCTION-STABLE-RANKING-SCHEDULER-EXPORT-V2
# ------------------------------------------------------------
# Purpose:
#   core.scheduler_tasks から参照される
#   trading.ranking.scheduler.job_save_ranking
#   trading.ranking.scheduler.save_ranking_data_loop
#   を scheduler_core から再exportする互換モジュール。
#
# Important:
#   このファイルには処理本体を書かない。
#   本体は trading.ranking.scheduler_core に一本化する。
#
# Expected:
#   起動ログで以下になる:
#     [core.scheduler_tasks] resolved trading.ranking.scheduler.job_save_ranking
#
#   確認コマンド:
#     python -c "import trading.ranking.scheduler as s; print(s.__file__); print(s.job_save_ranking.__module__)"
#
#   期待値:
#     ...\trading\ranking\scheduler.py
#     trading.ranking.scheduler_core
# ============================================================

from __future__ import annotations

from trading.ranking.scheduler_core import (
    job_save_ranking,
    save_ranking_data_loop,
    force_save_ranking_full_once,
    force_save_ranking_fast_once,
    now_in_market_hours,
)


__all__ = [
    "job_save_ranking",
    "save_ranking_data_loop",
    "force_save_ranking_full_once",
    "force_save_ranking_fast_once",
    "now_in_market_hours",
]