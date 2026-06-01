from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)
_INSTALLED = False


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    try:
        os.environ["RANKING_ENTRY_RUNTIME_BUDGET_SEC"] = os.getenv("RANKING_ENTRY_FAST_RUNTIME_BUDGET_SEC", "25")
        os.environ["RANKING_ENTRY_BUILD_TIMEOUT_SEC"] = os.getenv("RANKING_ENTRY_FAST_BUILD_TIMEOUT_SEC", "30")
        os.environ["RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC"] = os.getenv("RANKING_ENTRY_FAST_CONTROLLER_TIMEOUT_SEC", "30")
        os.environ["RANKING_ENTRY_MAX_PENDING_PER_RUN"] = os.getenv("RANKING_ENTRY_FAST_MAX_PENDING_PER_RUN", "1")
        os.environ.setdefault("RANKING_ENTRY_TIMEOUT_COOLDOWN_SEC", "45")
        os.environ.setdefault("RANKING_ENTRY_TIMEOUT_COOLDOWN_MAX_SEC", "120")
        try:
            import trading.entry_exit.tasks as tasks
            tasks.RANKING_ENTRY_BUILD_TIMEOUT_SEC = float(os.environ["RANKING_ENTRY_BUILD_TIMEOUT_SEC"])
            tasks.RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC = float(os.environ["RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC"])
        except Exception:
            logger.debug("[RANKING ENTRY FAST BUDGET OVERRIDE] tasks constants not ready", exc_info=True)
        _INSTALLED = True
        logger.warning(
            "[RANKING ENTRY FAST BUDGET OVERRIDE] installed runtime_budget=%s build_timeout=%s controller_timeout=%s max_pending=%s",
            os.environ.get("RANKING_ENTRY_RUNTIME_BUDGET_SEC"),
            os.environ.get("RANKING_ENTRY_BUILD_TIMEOUT_SEC"),
            os.environ.get("RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC"),
            os.environ.get("RANKING_ENTRY_MAX_PENDING_PER_RUN"),
        )
        return True
    except Exception:
        logger.exception("[RANKING ENTRY FAST BUDGET OVERRIDE] install failed")
        return False

try:
    install()
except Exception:
    logger.exception("[RANKING ENTRY FAST BUDGET OVERRIDE] auto install failed")

__all__ = ["install"]
