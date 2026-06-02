# ============================================================
# File   : core/startup/tonosama_controller_timeout_extend_patch.py
# Version: V1-TONOSAMA-CONTROLLER-TIMEOUT-EXTEND
# ------------------------------------------------------------
# Purpose:
#   Tonosama pending is created, but entry_controller dispatch times out
#   at 12 seconds. The controller thread keeps running and holds the
#   entry_controller lock, causing SUMMARY to hit entry_controller_lock_timeout.
#
# Fix:
#   Patch trading.entry_exit.tasks constants after import.
# ============================================================
from __future__ import annotations

import logging
import os
import threading
import time

logger = logging.getLogger(__name__)
_INSTALLED = False
_INSTALLING = False


def _f(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _apply() -> bool:
    global _INSTALLED
    try:
        import trading.entry_exit.tasks as tasks
        old_build = getattr(tasks, "TONOSAMA_ENTRY_TIMEOUT_SEC", None)
        old_ctrl = getattr(tasks, "TONOSAMA_ENTRY_CONTROLLER_TIMEOUT_SEC", None)
        old_cd = getattr(tasks, "TONOSAMA_ENTRY_TIMEOUT_COOLDOWN_SEC", None)
        old_cdmax = getattr(tasks, "TONOSAMA_ENTRY_TIMEOUT_COOLDOWN_MAX_SEC", None)
        os.environ.setdefault("TONOSAMA_ENTRY_TIMEOUT_SEC", "45")
        os.environ.setdefault("TONOSAMA_ENTRY_CONTROLLER_TIMEOUT_SEC", "45")
        os.environ.setdefault("TONOSAMA_DISPATCH_CONTROLLER_ON_TIMEOUT_PENDING", "1")
        os.environ.setdefault("TONOSAMA_ENTRY_TIMEOUT_COOLDOWN_SEC", "10")
        os.environ.setdefault("TONOSAMA_ENTRY_TIMEOUT_COOLDOWN_MAX_SEC", "60")
        tasks.TONOSAMA_ENTRY_TIMEOUT_SEC = max(float(old_build or 0), _f("TONOSAMA_ENTRY_TIMEOUT_SEC", 45.0), 45.0)
        tasks.TONOSAMA_ENTRY_CONTROLLER_TIMEOUT_SEC = max(float(old_ctrl or 0), _f("TONOSAMA_ENTRY_CONTROLLER_TIMEOUT_SEC", 45.0), 45.0)
        tasks.TONOSAMA_ENTRY_TIMEOUT_COOLDOWN_SEC = min(float(old_cd or 10.0), _f("TONOSAMA_ENTRY_TIMEOUT_COOLDOWN_SEC", 10.0), 10.0)
        tasks.TONOSAMA_ENTRY_TIMEOUT_COOLDOWN_MAX_SEC = min(float(old_cdmax or 60.0), _f("TONOSAMA_ENTRY_TIMEOUT_COOLDOWN_MAX_SEC", 60.0), 60.0)
        _INSTALLED = True
        logger.warning(
            "[TONOSAMA CONTROLLER TIMEOUT EXTEND] installed build %s->%.1f controller %s->%.1f dispatch_timeout_pending=%s cooldown %.1f/%.1f",
            old_build,
            tasks.TONOSAMA_ENTRY_TIMEOUT_SEC,
            old_ctrl,
            tasks.TONOSAMA_ENTRY_CONTROLLER_TIMEOUT_SEC,
            os.getenv("TONOSAMA_DISPATCH_CONTROLLER_ON_TIMEOUT_PENDING"),
            tasks.TONOSAMA_ENTRY_TIMEOUT_COOLDOWN_SEC,
            tasks.TONOSAMA_ENTRY_TIMEOUT_COOLDOWN_MAX_SEC,
        )
        return True
    except Exception:
        logger.debug("[TONOSAMA CONTROLLER TIMEOUT EXTEND] target not ready", exc_info=True)
        return False


def install(retry: bool = True) -> bool:
    global _INSTALLING
    if _INSTALLED:
        return True
    if _apply():
        return True
    if retry and not _INSTALLING:
        _INSTALLING = True

        def _loop() -> None:
            global _INSTALLING
            try:
                for _ in range(120):
                    if _apply():
                        return
                    time.sleep(0.25)
                logger.warning("[TONOSAMA CONTROLLER TIMEOUT EXTEND] retry exhausted")
            finally:
                _INSTALLING = False

        threading.Thread(target=_loop, name="tonosama-controller-timeout-extend", daemon=True).start()
    return False


try:
    install()
except Exception:
    logger.exception("[TONOSAMA CONTROLLER TIMEOUT EXTEND] auto install failed")


__all__ = ["install"]
