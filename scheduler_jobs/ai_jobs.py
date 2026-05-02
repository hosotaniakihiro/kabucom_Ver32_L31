# ============================================================
# File   : scheduler_jobs/ai_jobs.py
# Version: Ver1.0-AI-JOBS-PRODUCTION
# ------------------------------------------------------------
# ✔ entry AI enrichment
# ✔ AI scoring injection
# ✔ pending_entries AI補強
# ✔ global_data互換
# ✔ exception safe
# ✔ production safe
# ============================================================

from __future__ import annotations

import logging

from global_state import global_data

# ============================================================
# AI Engines
# ============================================================

from trading.entry.ai_enricher import (
    enrich_pending_entries_with_ai,
)

logger = logging.getLogger(__name__)


# ============================================================
# AI Entry Enrichment
# ============================================================

def job_ai_inject():
    """
    pending entry に AIスコアを付加
    """

    try:

        enrich_pending_entries_with_ai()

        pending = getattr(global_data, "pending_entries", None)

        if pending:

            logger.info(
                "[AI] enriched pending_entries=%s",
                len(pending),
            )

        else:

            logger.debug(
                "[AI] pending_entries empty"
            )

    except Exception:

        logger.exception("[job_ai_inject]")


# ============================================================
# AI Feature Refresh（将来用）
# ============================================================

def job_ai_feature_refresh():
    """
    AI feature refresh placeholder
    将来 feature store 更新に使用
    """

    try:

        # placeholder
        pass

    except Exception:

        logger.exception("[job_ai_feature_refresh]")