# ============================================================
# File   : trading/ranking/summary/persistence/writer.py
# Version: COMPAT-REV4.0-DELEGATE-TO-DATABASE
# ============================================================

from __future__ import annotations

from database.upsert.ranking_summary_upsert import save_ranking_summary

__all__ = [
    "save_ranking_summary",
]