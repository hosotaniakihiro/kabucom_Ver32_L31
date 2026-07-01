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

try:
    from core.startup.push_stream_data_received_at_datetime_patch import install as _install_push_received_at

    _push_received_at_ok = bool(_install_push_received_at())
    logging.getLogger(__name__).warning(
        "[PUSH RECEIVER RUNNER] PUSH_STREAM_DATA_RECEIVED_AT_DATETIME forced install ok=%s",
        _push_received_at_ok,
    )
except Exception:
    logging.getLogger(__name__).exception(
        "[PUSH RECEIVER RUNNER] PUSH_STREAM_DATA_RECEIVED_AT_DATETIME forced install failed"
    )

try:
    from core.startup.kabusapi_token_retry_register_patch import install as _install_token_retry

    _token_retry_ok = bool(_install_token_retry())
    logging.getLogger(__name__).warning(
        "[PUSH RECEIVER RUNNER] KABUSAPI_TOKEN_RETRY_REGISTER forced install ok=%s",
        _token_retry_ok,
    )
except Exception:
    logging.getLogger(__name__).exception(
        "[PUSH RECEIVER RUNNER] KABUSAPI_TOKEN_RETRY_REGISTER forced install failed"
    )

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
