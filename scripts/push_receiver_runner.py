# ============================================================
# File   : scripts/push_receiver_runner.py
# Version: DATA-COLLECTORS-PUSH-RECEIVER-RUNNER-V2-TOKEN-RETRY
# ============================================================

from __future__ import annotations

import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Install this before importing the long-running PUSH runtime.  usercustomize.py
# also tries to install it, but this explicit import guarantees that the child
# process handling /register and /unregister/all can refresh the kabu Station
# token after Code 4001009 / APIキー不一致 and retry once.
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
