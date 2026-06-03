# ============================================================
# File   : core/startup/tonosama_runtime_25sec_budget_patch.py
# Version: V1-TONOSAMA-SCHEDULER-25SEC-BUDGET
# ------------------------------------------------------------
# Purpose:
#   Tonosama scheduler is usually registered every 30 seconds.  Logs showed
#   a previous run still active at the next tick.  Keep the build/controller
#   budget below the interval so the scheduler can recover quickly.
# ============================================================
from __future__ import annotations

import logging
import os
import threading
import time

logger = logging.getLogger(__name__)
_INSTALLED = False
_INSTALLING = False


def _set_default_env() -> None:
    # Build body should return to scheduler before the next 30s tick.
    os.environ.setdefault("TONOSAMA_ENTRY_TIMEOUT_SEC", "25")
    os.environ.setdefault("TONOSAMA_ENTRY_CONTROLLER_TIMEOUT_SEC", "8")
    os.environ.setdefault("TONOSAMA_ENTRY_TIMEOUT_COOLDOWN_SEC", "30")
    os.environ.setdefault("TONOSAMA_ENTRY_TIMEOUT_COOLDOWN_MAX_SEC", "120")

    # Keep existing speed guards enabled.
    os.environ.setdefault("TONOSAMA_ENTRY_FAST_SKIP_ACTIVE_UPDATE", "1")
    os.environ.setdefault("TONOSAMA_DISPATCH_CONTROLLER_ON_TIMEOUT_PENDING", "1")


def _apply() -> bool:
    _set_default_env()
    try:
        import trading.entry_exit.tasks as tasks
    except Exception:
        logger.debug("[TONOSAMA 25SEC BUDGET] tasks not ready", exc_info=True)
        return False

    try:
        # tasks.py computes constants at import time, so overwrite them too.
        tasks.TONOSAMA_ENTRY_TIMEOUT_SEC = 25.0
        tasks.TONOSAMA_ENTRY_CONTROLLER_TIMEOUT_SEC = max(8.0, float(os.getenv("TONOSAMA_ENTRY_CONTROLLER_TIMEOUT_SEC", "8") or 8.0))
        tasks.TONOSAMA_ENTRY_TIMEOUT_COOLDOWN_SEC = max(1.0, float(os.getenv("TONOSAMA_ENTRY_TIMEOUT_COOLDOWN_SEC", "30") or 30.0))
        tasks.TONOSAMA_ENTRY_TIMEOUT_COOLDOWN_MAX_SEC = max(tasks.TONOSAMA_ENTRY_TIMEOUT_COOLDOWN_SEC, float(os.getenv("TONOSAMA_ENTRY_TIMEOUT_COOLDOWN_MAX_SEC", "120") or 120.0))
        logger.warning(
            "[TONOSAMA 25SEC BUDGET] applied build_timeout=%.1f controller_timeout=%.1f cooldown=%.1f max_cooldown=%.1f",
            tasks.TONOSAMA_ENTRY_TIMEOUT_SEC,
            tasks.TONOSAMA_ENTRY_CONTROLLER_TIMEOUT_SEC,
            tasks.TONOSAMA_ENTRY_TIMEOUT_COOLDOWN_SEC,
            tasks.TONOSAMA_ENTRY_TIMEOUT_COOLDOWN_MAX_SEC,
        )
        return True
    except Exception:
        logger.exception("[TONOSAMA 25SEC BUDGET] apply failed")
        return False


def install(retry: bool = True) -> bool:
    global _INSTALLED, _INSTALLING
    if _INSTALLED:
        return _apply()
    if _apply():
        _INSTALLED = True
        return True
    if retry and not _INSTALLING:
        _INSTALLING = True

        def _loop() -> None:
            global _INSTALLED, _INSTALLING
            try:
                for _ in range(120):
                    if _apply():
                        _INSTALLED = True
                        logger.warning("[TONOSAMA 25SEC BUDGET] installed by retry")
                        return
                    time.sleep(0.25)
                logger.warning("[TONOSAMA 25SEC BUDGET] retry exhausted")
            finally:
                _INSTALLING = False

        threading.Thread(target=_loop, name="tonosama-25sec-budget-install", daemon=True).start()
    return False


try:
    install()
except Exception:
    logger.exception("[TONOSAMA 25SEC BUDGET] auto install failed")


__all__ = ["install"]
