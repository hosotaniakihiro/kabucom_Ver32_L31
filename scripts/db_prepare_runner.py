# ============================================================
# File   : scripts/db_prepare_runner.py
# Version: DATA-COLLECTORS-DB-PREPARE-RUNNER-V1
# ============================================================

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from data_collectors.db_prepare import prepare_all_daily_dbs
from data_collectors.logging_setup import setup_logging


def main() -> int:
    logger = setup_logging("db_prepare_runner")
    logger.info("[DB PREPARE RUNNER] START")
    try:
        prepare_all_daily_dbs()
        logger.info("[DB PREPARE RUNNER] DONE")
        return 0
    except Exception:
        logger.exception("[DB PREPARE RUNNER] FAILED")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
