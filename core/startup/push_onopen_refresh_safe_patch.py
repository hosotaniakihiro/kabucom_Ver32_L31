# ============================================================
# File   : core/startup/push_onopen_refresh_safe_patch.py
# Version: V4-COMPAT-ENV-ONLY-CORE-INTEGRATED
# ------------------------------------------------------------
# Previously this patch disabled on_open refresh and installed extra PUSH
# rotation wait patches by monkey-patching transport/ws_callbacks.
#
# The PUSH core now owns this behaviour:
#   - on_open refresh is skipped by env/default policy
#   - A/B rotation owns register
#   - rotation_core waits for WebSocket readiness before hold
#
# Keep this as a compatibility/env-default shim only.
# ============================================================
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)
_INSTALLED = False


def _set_default(name: str, value: str) -> None:
    try:
        if os.environ.get(name) is None or str(os.environ.get(name)).strip() == "":
            os.environ[name] = str(value)
    except Exception:
        pass


def _force_default(name: str, value: str) -> None:
    try:
        os.environ[name] = str(value)
    except Exception:
        pass


def _apply_defaults() -> None:
    # Registration is owned by A/B rotation, not on_open.
    _force_default("PUSH_STREAM_SKIP_AFTER_OPEN_REFRESH", "1")
    _force_default("PUSH_STREAM_ONOPEN_REFRESH_CLEAR_FIRST", "0")
    _force_default("PUSH_STREAM_ONOPEN_REFRESH_UNREGISTER_FIRST", "0")
    _force_default("PUSH_STREAM_ONOPEN_REFRESH_WAIT_AFTER_CLEAR", "0.0")
    _set_default("PUSH_STREAM_ONOPEN_SAFE_REFRESH_DELAY_SEC", "3.0")
    _set_default("PUSH_STREAM_ONOPEN_SAFE_REFRESH_MIN_INTERVAL_SEC", "15.0")
    _set_default("PUSH_STREAM_ONOPEN_SAFE_READY_WAIT_SEC", "4.0")
    _set_default("PUSH_STREAM_ONOPEN_SAFE_FORCE", "0")
    _set_default("PUSH_STREAM_ONOPEN_SAFE_CLEAR_FIRST", "0")
    _set_default("PUSH_STREAM_ONOPEN_SAFE_UNREGISTER_FIRST", "0")
    _set_default("PUSH_STREAM_ONOPEN_SAFE_WAIT_AFTER_CLEAR_SEC", "0.0")

    # Core rotation owns WS-ready-after-register now.
    _force_default("PUSH_ROTATION_WAIT_WS_READY_AFTER_REGISTER", "1")
    _force_default("PUSH_ROTATION_POST_REGISTER_WS_READY_TIMEOUT_SEC", "4.0")
    _force_default("PUSH_ROTATION_POST_REGISTER_WS_POLL_SEC", "0.05")
    _force_default("PUSH_ROTATION_POST_REGISTER_WS_SETTLE_SEC", "0.25")


def install(*_args, **_kwargs) -> bool:
    global _INSTALLED
    if not _INSTALLED:
        _apply_defaults()
        _INSTALLED = True
        logger.warning(
            "[PUSH ONOPEN SAFE REFRESH] core-integrated env-only shim v4 installed; "
            "no monkey patch applied"
        )
    return True


try:
    install()
except Exception:
    logger.exception("[PUSH ONOPEN SAFE REFRESH] env-only shim install failed")


__all__ = ["install"]
