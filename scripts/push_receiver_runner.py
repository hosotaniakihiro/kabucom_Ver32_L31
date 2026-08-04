# ============================================================
# File   : scripts/push_receiver_runner.py
# Version: DATA-COLLECTORS-PUSH-RECEIVER-RUNNER-V4-START-PUSH-STORAGE
# ============================================================

from __future__ import annotations

import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from data_collectors.logging_setup import setup_logging
from data_collectors.push_runtime import run_forever


def _start_push_storage_for_database_process(logger: logging.Logger) -> None:
    """
    PUSH DB保存は main.py では起動しない。
    main_database.py -> push_receiver_runner.py 側だけで、PUSH受信開始前に
    StreamDBWriter を明示起動する。

    これにより main.py 側は memory_only=True のまま維持しつつ、
    main_database.py 側では writer_ready/flush_alive/total_flushed が進む。
    """
    try:
        from core.startup.push_storage_bootstrap import start_push_storage

        start_push_storage(buffer_size=500)
        logger.warning(
            "[PUSH RECEIVER RUNNER] push storage start requested in database process only"
        )
    except Exception:
        logger.exception("[PUSH RECEIVER RUNNER] push storage start failed")


def main() -> int:
    logger = setup_logging("push_receiver_runner")
    logger.info("[PUSH RECEIVER RUNNER] START")
    _start_push_storage_for_database_process(logger)
    return run_forever()


if __name__ == "__main__":
    raise SystemExit(main())
