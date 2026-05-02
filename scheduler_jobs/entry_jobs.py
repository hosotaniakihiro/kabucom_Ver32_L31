# ============================================================
# File   : scheduler_jobs/entry_jobs.py
# Version: Ver1.0-ENTRY-JOBS-PRODUCTION
# ------------------------------------------------------------
# ✔ active symbols 更新
# ✔ entry pipeline 実行
# ✔ AI entry enrichment
# ✔ global_data互換
# ✔ exception safe
# ✔ production safe
# ============================================================

from __future__ import annotations

import logging

from global_state import global_data

# ============================================================
# Entry Engines
# ============================================================

from trading.ranking.active_symbol_manager import (
    update_active_symbols,
)

from trading.handlers.entry_controller import (
    run_entry_pipeline,
)

from trading.entry.ai_enricher import (
    enrich_pending_entries_with_ai,
)

logger = logging.getLogger(__name__)


# ============================================================
# Active Symbols Update
# ============================================================

def job_update_active_symbols():
    """
    ATS監視銘柄更新
    """

    try:

        update_active_symbols()

        active = getattr(global_data, "symbols_active", None)

        if active:

            logger.info(
                "[ACTIVE_SYMBOLS] updated count=%s",
                len(active),
            )

        else:

            logger.info(
                "[ACTIVE_SYMBOLS] empty"
            )

    except Exception:

        logger.exception("[job_update_active_symbols]")


# ============================================================
# Entry Pipeline
# ============================================================

def job_run_entry_pipeline():
    """
    エントリー実行
    """

    try:

        active = getattr(global_data, "symbols_active", None)

        if not active:

            logger.info("[ENTRY] skipped (no active symbols)")
            return

        run_entry_pipeline()

    except Exception:

        logger.exception("[job_run_entry_pipeline]")


# ============================================================
# AI Entry Enrichment
# ============================================================

def job_ai_inject():
    """
    pending entry に AI を付加
    """

    try:

        enrich_pending_entries_with_ai()

    except Exception:

        logger.exception("[job_ai_inject]")