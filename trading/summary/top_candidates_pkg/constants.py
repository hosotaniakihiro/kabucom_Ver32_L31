# ============================================================
# File   : trading/summary/top_candidates_pkg/constants.py
# Version: Ver2.2-PRODUCTION-SUMMARY-TOP-CANDIDATES-CONSTANTS
# ------------------------------------------------------------
# Function:
#   - top_candidates 共通定数
# ============================================================

from __future__ import annotations

from typing import Tuple

DEFAULT_INTERVALS: Tuple[int, ...] = (1, 3, 5)
DEFAULT_SIDES: Tuple[str, ...] = ("BUY", "SELL")

PUSH_SOURCE = "push_summary"
RANKING_SOURCE = "ranking_summary"