# ============================================================
# File   : trading/summary/top_candidates_pkg/__init__.py
# Version: Ver2.2-PRODUCTION-SUMMARY-TOP-CANDIDATES-PACKAGE
# ------------------------------------------------------------
# Function:
#   - top_candidates 分割パッケージの公開入口
#   - facade から再エクスポートされる公開APIを集約
# ============================================================

from __future__ import annotations

from .legacy_top import (
    prepare_buy_sell_top_df,
    prepare_buy_top_df,
    prepare_sell_top_df,
)

from .collectors import (
    collect_push_summary_candidates,
    collect_ranking_summary_candidates,
    collect_ai_entry_candidates,
    collect_top_candidates_for_ai,
)

from .merger import (
    merge_ai_entry_candidates,
)

from .diagnostics import (
    log_ai_entry_candidates,
)

__all__ = [
    "prepare_buy_sell_top_df",
    "prepare_buy_top_df",
    "prepare_sell_top_df",
    "collect_push_summary_candidates",
    "collect_ranking_summary_candidates",
    "merge_ai_entry_candidates",
    "collect_ai_entry_candidates",
    "collect_top_candidates_for_ai",
    "log_ai_entry_candidates",
]