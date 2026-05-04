# ============================================================
# File   : trading/summary/summary_pipeline.py
# Version: Ver26-PRODUCTION-SUMMARY-PIPELINE-WRAPPER
# ------------------------------------------------------------
# ✔ pipeline.summary_pipeline への互換ラッパー
# ✔ 旧import互換維持
# ✔ 本体ロジックは pipeline 側
# ✔ circular import防止
# ✔ production safe
# ============================================================

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


# ============================================================
# IMPORT REAL PIPELINE
# ============================================================

try:

    from trading.summary.pipeline.summary_pipeline import (
        rebuild_summary_from_db_and_push,
    )

except Exception as e:

    logger.exception(
        "[SUMMARY PIPELINE WRAPPER] pipeline import failed"
    )

    def rebuild_summary_from_db_and_push(*args, **kwargs):
        raise RuntimeError(
            "summary pipeline implementation not available"
        ) from e


# ============================================================
# OPTIONAL WRAPPER API
# ============================================================

def run_summary_pipeline(*args, **kwargs):
    """
    summary_engine 互換API
    """

    return rebuild_summary_from_db_and_push(*args, **kwargs)