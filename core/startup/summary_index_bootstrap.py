# ============================================================
# File   : core/startup/summary_index_bootstrap.py
# Version: REV1.1-SKIP-UNIQUE-INDEX-IN-MAIN
# ------------------------------------------------------------
# 【概要】
#   summary unique index bootstrap を startup.py から分離
#
# REV1.1:
#   - main.py では summary unique index bootstrap を既定スキップ
#   - main_database.py / data collectors 側で index 作成・確認を担当
#   - main.pyで強制実行したい場合のみ SUMMARY_UNIQUE_INDEX_RUN_IN_MAIN=1
# ============================================================

from __future__ import annotations

import logging
import os
import sys

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


def _env_bool(name: str, default: bool = False) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _is_main_py_process() -> bool:
    try:
        argv = [str(x).replace("\\", "/").lower() for x in sys.argv]
        return any(x.endswith("/main.py") or x == "main.py" for x in argv)
    except Exception:
        return False


def _is_database_process() -> bool:
    return any(
        _env_bool(name, False)
        for name in (
            "AUTOSTOCK_DATA_COLLECTORS_PROCESS",
            "AUTOSTOCK_SUMMARY_DB_WRITER",
            "AUTOSTOCK_MAIN_DATABASE_PROCESS",
        )
    )


def _should_skip_in_main() -> bool:
    if not _is_main_py_process():
        return False
    if _is_database_process():
        return False
    if _env_bool("SUMMARY_UNIQUE_INDEX_RUN_IN_MAIN", False):
        return False
    return _env_bool("SUMMARY_UNIQUE_INDEX_SKIP_IN_MAIN", True)


def bootstrap_summary_unique_indexes_safe() -> None:
    set_summary_unique_index_bootstrap_flags(
        started=True,
        done=False,
        failed=False,
        results=None,
    )

    if _should_skip_in_main():
        logger.warning(
            "[STARTUP] summary unique index bootstrap skipped in main.py. "
            "main_database.py handles summary indexes. "
            "set SUMMARY_UNIQUE_INDEX_RUN_IN_MAIN=1 to force."
        )
        set_summary_unique_index_bootstrap_flags(
            started=True,
            done=True,
            failed=False,
            results={"skipped_in_main": True},
        )
        return

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
