# ============================================================
# File   : scripts/push_receiver_runner.py
# Version: DATA-COLLECTORS-PUSH-RECEIVER-RUNNER-V3-PUSH-RECEIVED-AT-DATETIME
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


def main() -> int:
    logger = setup_logging("push_receiver_runner")
    logger.info("[PUSH RECEIVER RUNNER] START")
    return run_forever()


if __name__ == "__main__":
    raise SystemExit(main())
