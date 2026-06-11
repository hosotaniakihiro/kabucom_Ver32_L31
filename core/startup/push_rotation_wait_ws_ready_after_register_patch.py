# ============================================================
# File   : core/startup/push_rotation_wait_ws_ready_after_register_patch.py
# Version: V3-COMPAT-NOOP-CORE-INTEGRATED
# ------------------------------------------------------------
# This patch used to monkey-patch PUSH rotation so that:
#   - rotation register could run while WebSocket was intentionally closed
#   - rotation_register._call_refresh was rebound to transport._call_refresh
#   - rotation_core waited for WebSocket readiness before starting the hold
#
# These behaviours are now integrated directly into:
#   - trading.push.push_stream.transport
#   - trading.push.push_stream.rotation_register
#   - trading.push.push_stream.rotation_core
#
# Keep this module as a compatibility shim because usercustomize and
# push_onopen_refresh_safe_patch may still import/install it.  Returning True
# avoids startup noise while preventing double wrapping.
# ============================================================
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)
_INSTALLED = False


def install(*_args, **_kwargs) -> bool:
    global _INSTALLED
    if not _INSTALLED:
        _INSTALLED = True
        logger.warning(
            "[PUSH ROTATION WAIT WS] skipped: core-integrated no-op v3 "
            "(transport/rotation_register/rotation_core own this logic)"
        )
    return True


try:
    install()
except Exception:
    logger.exception("[PUSH ROTATION WAIT WS] compat no-op install failed")


__all__ = ["install"]
