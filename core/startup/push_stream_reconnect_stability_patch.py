# ============================================================
# File   : core/startup/push_stream_reconnect_stability_patch.py
# Version: V13-COMPAT-ENV-ONLY-CORE-INTEGRATED
# ------------------------------------------------------------
# Previously this monkey-patched:
#   - transport._call_refresh
#   - runner._run_forever_loop
#   - rotation_core._run_rotation_side
#
# These behaviours are now integrated directly into the PUSH core:
#   - trading.push.push_stream.transport
#   - trading.push.push_stream.runner
#   - trading.push.push_stream.rotation_core
#   - trading.push.push_stream.rotation_register
#
# Keep this module only as a compatibility/env-default shim while
# usercustomize still imports it.  It must not wrap core functions again.
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
        # Keep the previous operational defaults, but do not monkey patch code.
        os.environ[name] = str(value)
    except Exception:
        pass


def _apply_env_defaults() -> None:
    # on_open REST refresh is disabled; A/B rotation owns unregister_all/register.
    _force_default("PUSH_STREAM_SKIP_AFTER_OPEN_REFRESH", "1")
    _force_default("PUSH_STREAM_ONOPEN_REFRESH_CLEAR_FIRST", "0")
    _force_default("PUSH_STREAM_ONOPEN_REFRESH_UNREGISTER_FIRST", "0")
    _force_default("PUSH_STREAM_ONOPEN_REFRESH_WAIT_AFTER_CLEAR", "0.0")
    _force_default("PUSH_STREAM_AFTER_OPEN_REFRESH_DELAY_SEC", "0.50")
    _force_default("PUSH_STREAM_ONOPEN_WS_READY_TIMEOUT_SEC", "1.0")

    # Vendor sample mode is now handled by runner.py itself.
    _force_default("PUSH_WS_VENDOR_RUN_FOREVER", "1")
    _force_default("PUSH_WS_ENABLE_PING", "0")
    _set_default("PUSH_WS_PING_INTERVAL_SEC", "20")
    _set_default("PUSH_WS_PING_TIMEOUT_SEC", "10")

    # Vendor-safe rotation behaviour is now handled by rotation_core/transport.
    _force_default("PUSH_ROTATION_CLOSE_WS_BEFORE_REGISTER", "1")
    _force_default("PUSH_ROTATION_REGISTER_WITH_WS_CLOSED", "1")
    _force_default("PUSH_STREAM_PAUSE_RECONNECT_DURING_REGISTER", "1")
    _force_default("PUSH_ROTATION_WS_CLOSE_SETTLE_SEC", "0.15")
    _force_default("PUSH_ROTATION_UNREGISTER_WAIT_SEC", "0.2")

    # Fast reconnect after vendor close/reset.
    _force_default("PUSH_STREAM_RECONNECT_BACKOFF_BASE_SEC", "0.3")
    _force_default("PUSH_STREAM_RECONNECT_BACKOFF_MAX_SEC", "1.0")
    _force_default("PUSH_STREAM_RECONNECT_STABLE_RESET_SEC", "5.0")
    _force_default("PUSH_STREAM_SHORT_LIVED_SEC", "1.0")
    _force_default("PUSH_STREAM_SHORT_LIVED_EXTRA_COOLDOWN_SEC", "0.0")

    # Leave owner-lock environment for push_main_owner_policy_patch.
    _set_default("PUSH_STREAM_SINGLE_OWNER_LOCK", "1")
    _set_default("PUSH_STREAM_SINGLE_OWNER_WAIT_RETRY", "1")
    _set_default("PUSH_STREAM_SINGLE_OWNER_RETRY_SEC", "1.0")
    _set_default("PUSH_STREAM_SINGLE_OWNER_LOG_EVERY_SEC", "20.0")
    _set_default("PUSH_STREAM_EMPTY_OWNER_LOCK_FAIL_OPEN", "1")
    _set_default("PUSH_STREAM_EMPTY_OWNER_LOCK_REQUIRE_OWNER_CONTEXT", "1")


def install(*_args, **_kwargs) -> bool:
    global _INSTALLED
    if not _INSTALLED:
        _apply_env_defaults()
        _INSTALLED = True
        logger.warning(
            "[PUSH RECONNECT STABILITY] core-integrated env-only shim v13 installed; "
            "no monkey patch applied"
        )
    return True


try:
    install()
except Exception:
    logger.exception("[PUSH RECONNECT STABILITY] env-only shim install failed")


__all__ = ["install"]
