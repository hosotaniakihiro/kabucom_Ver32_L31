from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)
_INSTALLED = False
_ORIG = None


def _patched_log_skip(symbol: Any, skip_reason: Any = None, *args, **kwargs):
    if "reason" in kwargs:
        kwargs.setdefault("detail_reason", kwargs.pop("reason"))
    return _ORIG(symbol, skip_reason, *args, **kwargs)  # type: ignore[misc]


def install() -> bool:
    global _INSTALLED, _ORIG
    if _INSTALLED:
        return True
    try:
        import trading.handlers.entry_controller as ec
        cur = getattr(ec, "_log_skip", None)
        if not callable(cur):
            logger.warning("[ENTRY LOG SKIP GUARD] _log_skip unavailable")
            return False
        if getattr(cur, "_entry_log_skip_guard_v1", False):
            _INSTALLED = True
            return True
        _ORIG = cur
        _patched_log_skip._entry_log_skip_guard_v1 = True  # type: ignore[attr-defined]
        _patched_log_skip._original = cur  # type: ignore[attr-defined]
        ec._log_skip = _patched_log_skip
        _INSTALLED = True
        logger.warning("[ENTRY LOG SKIP GUARD] installed")
        return True
    except Exception:
        logger.exception("[ENTRY LOG SKIP GUARD] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[ENTRY LOG SKIP GUARD] auto install failed")

__all__ = ["install"]
