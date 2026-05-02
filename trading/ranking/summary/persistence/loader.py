# ============================================================
# File   : trading/ranking/summary/persistence/loader.py
# Version: COMPAT-REV4.0-DELEGATE-TO-DATABASE
# ============================================================

from __future__ import annotations

from database.loader.ranking_summary_loader import (
    get_ranking_summary_schema_columns,
    load_latest_ranking_summary,
    load_ranking_summary_at_latest_slot,
)

__all__ = [
    "load_latest_ranking_summary",
    "load_ranking_summary_at_latest_slot",
    "get_ranking_summary_schema_columns",
]