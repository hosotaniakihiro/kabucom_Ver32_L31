# ============================================================
# File   : core/startup/summary_index_bootstrap.py
# Version: REV1.0-SUMMARY-INDEX-BOOTSTRAP
# ------------------------------------------------------------
# 【概要】
#   summary unique index bootstrap を startup.py から分離
#
# 【主な機能】
#   - bootstrap_summary_unique_indexes availability guard
#   - global_data flags 更新
# ============================================================

from __future__ import annotations

import logging

from core.startup.startup_flags import set_summary_unique_index_bootstrap_flags

logger = logging.getLogger(__name__)

try:
    from trading.summary.persistence.core.upsert_bootstrap import (
        bootstrap_summary_unique_indexes,
    )

    _UPSERT_BOOTSTRAP_AVAILABLE = True
except Exception:
    bootstrap_summary_unique_indexes = None
    _UPSERT_BOOTSTRAP_AVAILABLE = False


def bootstrap_summary_unique_indexes_safe() -> None:
    set_summary_unique_index_bootstrap_flags(
        started=True,
        done=False,
        failed=False,
        results=None,
    )

    if not _UPSERT_BOOTSTRAP_AVAILABLE or bootstrap_summary_unique_indexes is None:
        logger.warning(
            "[STARTUP] summary unique index bootstrap unavailable -> skip "
            "(live path must not create indexes)"
        )
        set_summary_unique_index_bootstrap_flags(
            started=True,
            done=False,
            failed=True,
            results={},
        )
        return

    try:
        logger.info("🧱 summary unique index bootstrap start intervals=(1,3,5)")
        results = bootstrap_summary_unique_indexes(intervals=(1, 3, 5))
        set_summary_unique_index_bootstrap_flags(
            started=True,
            done=True,
            failed=False,
            results=results,
        )
        logger.info("✅ summary unique index bootstrap complete results=%s", results)

    except Exception:
        set_summary_unique_index_bootstrap_flags(
            started=True,
            done=False,
            failed=True,
            results={},
        )
        logger.exception("❌ summary unique index bootstrap failed")
        raise


__all__ = [
    "bootstrap_summary_unique_indexes_safe",
]
