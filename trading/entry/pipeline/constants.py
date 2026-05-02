# ============================================================
# File   : trading/entry/pipeline/constants.py
# Function:
#   - entry pipeline 共通定数
# ------------------------------------------------------------
# Version: Ver39-PRODUCTION-ENTRY-PIPELINE-CONSTANTS
# ============================================================

from __future__ import annotations

SUMMARY_BUY_TOP_N = 10
SUMMARY_SELL_TOP_N = 10

AI_ENTRY_TOP_N = 10
AI_ENTRY_MAX_TOTAL = 30

AI_ENTRY_INTERVALS_DEFAULT = (1, 3, 5)
AI_ENTRY_SIDES_DEFAULT = ("BUY", "SELL")

SOURCE_SUMMARY = "summary"
SOURCE_RANKING = "ranking"
SOURCE_AI = "ai"
SOURCE_COMBINED = "combined"

SOURCE_PUSH_SUMMARY = "push_summary"
SOURCE_RANKING_SUMMARY = "ranking_summary"