# ============================================================
# File   : scripts/ranking_collector_runner.py
# Version: DATA-COLLECTORS-RANKING-COLLECTOR-RUNNER-V1
# ============================================================

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from data_collectors.logging_setup import setup_logging
from data_collectors.ranking_runtime import run_forever


def main() -> int:
    logger = setup_logging("ranking_collector_runner")
    logger.info("[RANKING COLLECTOR RUNNER] START")
    return run_forever()


if __name__ == "__main__":
    raise SystemExit(main())
