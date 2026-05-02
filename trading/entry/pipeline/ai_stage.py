# ============================================================
# File   : trading/entry/pipeline/ai_stage.py
# Function:
#   - pending_entries に対する AI enrich
#   - entry_controller への接続
# ------------------------------------------------------------
# Version: Ver39-PRODUCTION-ENTRY-PIPELINE-AI-STAGE
# ============================================================

from __future__ import annotations

import logging
from typing import Any, Optional

from .imports import (
    enrich_pending_entries_with_ai,
    run_entry_controller,
)

from .pending_bridge import (
    count_pending_stats,
    log_pending_detail,
)

logger = logging.getLogger(__name__)


def run_ai_enrich_and_entry_controller(
    *,
    pipeline_source: str,
    interval: Optional[int],
) -> Any:
    """
    pending_entries に対して AI enrich を行い、entry_controller へ渡す。
    """

    try:
        before_symbols, before_entries, before_allow = count_pending_stats()

        logger.info(
            "[ENTRY PIPELINE][%s] before AI enrich symbols=%d entries=%d ai_allow=%d",
            pipeline_source,
            before_symbols,
            before_entries,
            before_allow,
        )

        if before_entries <= 0:
            logger.info("[ENTRY PIPELINE][%s] no pending entries before AI enrich", pipeline_source)
            return None

        if enrich_pending_entries_with_ai is not None:
            try:
                enrich_pending_entries_with_ai()
            except Exception:
                logger.exception("[ENTRY PIPELINE][%s] AI enrich failed", pipeline_source)
        else:
            logger.warning("[ENTRY PIPELINE][%s] AI enricher unavailable", pipeline_source)

        after_symbols, after_entries, after_allow = count_pending_stats()

        logger.info(
            "[ENTRY PIPELINE][%s] after AI enrich symbols=%d entries=%d ai_allow=%d",
            pipeline_source,
            after_symbols,
            after_entries,
            after_allow,
        )

        log_pending_detail(limit=10)

        if run_entry_controller is None:
            logger.warning("[ENTRY PIPELINE][%s] entry_controller unavailable", pipeline_source)
            return None

        if interval is None:
            return run_entry_controller(
                pipeline_source=pipeline_source,
            )

        return run_entry_controller(
            pipeline_source=pipeline_source,
            interval=int(interval),
        )

    except Exception:
        logger.exception("[ENTRY PIPELINE][%s] AI enrich/controller failed", pipeline_source)
        return None