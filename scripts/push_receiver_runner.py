# ============================================================
# File   : scripts/push_receiver_runner.py
# Version: DATA-COLLECTORS-PUSH-RECEIVER-RUNNER-V1
# ============================================================

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from data_collectors.logging_setup import setup_logging
from data_collectors.push_runtime import run_forever


def main() -> int:
    logger = setup_logging("push_receiver_runner")
    logger.info("[PUSH RECEIVER RUNNER] START")
    return run_forever()


if __name__ == "__main__":
    raise SystemExit(main())
