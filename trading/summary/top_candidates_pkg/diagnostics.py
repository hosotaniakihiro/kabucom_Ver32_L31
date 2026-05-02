# ============================================================
# File   : trading/summary/top_candidates_pkg/diagnostics.py
# Version: Ver2.2-PRODUCTION-SUMMARY-TOP-CANDIDATES-DIAGNOSTICS
# ------------------------------------------------------------
# Function:
#   - AI-Gate に渡す候補の診断ログ
# ============================================================

from __future__ import annotations

import logging
from typing import Any, Dict, List

from .utils import safe_float

logger = logging.getLogger(__name__)


def log_ai_entry_candidates(
    candidates: List[Dict[str, Any]],
    *,
    prefix: str = "[AI CANDIDATES]",
    limit: int = 10,
) -> None:
    """
    AI-Gate に渡す候補の中身をログ表示する。
    run_entry_pipeline 側から呼んでもよい。
    """

    try:
        candidates = candidates or []

        total = len(candidates)
        push = sum(1 for x in candidates if x.get("has_push_summary"))
        ranking = sum(1 for x in candidates if x.get("has_ranking_summary"))
        both = sum(
            1
            for x in candidates
            if x.get("has_push_summary") and x.get("has_ranking_summary")
        )

        logger.info(
            "%s total=%d push=%d ranking=%d both=%d",
            prefix,
            total,
            push,
            ranking,
            both,
        )

        for i, c in enumerate(candidates[: int(limit)], start=1):
            logger.info(
                "%s #%02d symbol=%s name=%s side=%s sources=%s priority=%.2f reason=%s",
                prefix,
                i,
                c.get("symbol"),
                c.get("symbolname"),
                c.get("side"),
                c.get("matched_sources"),
                safe_float(c.get("ai_priority_score")),
                c.get("entry_priority_reason"),
            )

    except Exception:
        logger.exception("%s log failed", prefix)