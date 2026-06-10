from __future__ import annotations

import logging
import os
import threading
import time

logger = logging.getLogger(__name__)
_DONE = False
_LOCK = threading.Lock()
VERSION = "v1"


def _set_caps() -> bool:
    try:
        os.environ["RANKING_ENTRY_RUNTIME_BUDGET_SEC"] = "25.0"
        os.environ["RANKING_ENTRY_RUNTIME_WARN_SEC"] = "25.0"
        os.environ["RANKING_ENTRY_RUNTIME_STALE_SEC"] = "35.0"
        os.environ["RANKING_ENTRY_BUILD_TIMEOUT_SEC"] = "30.0"
        os.environ["RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC"] = "30.0"
        os.environ["RANKING_ENTRY_HARD_TIMEOUT_SEC"] = "35.0"
        os.environ["RANKING_ENTRY_MAX_PENDING_PER_RUN"] = "4"
        os.environ["RANKING_ENTRY_FAST_RUNTIME_BUDGET_SEC"] = "25.0"
        os.environ["RANKING_ENTRY_FAST_BUILD_TIMEOUT_SEC"] = "30.0"
        os.environ["RANKING_ENTRY_FAST_CONTROLLER_TIMEOUT_SEC"] = "30.0"
        os.environ["RANKING_ENTRY_FAST_MAX_PENDING_PER_RUN"] = "4"
        os.environ["RANKING_ENTRY_INTRADAY_RUNTIME_BUDGET_SEC"] = "25.0"
        os.environ["RANKING_ENTRY_INTRADAY_BUILD_TIMEOUT_SEC"] = "30.0"
        os.environ["RANKING_ENTRY_INTRADAY_CONTROLLER_TIMEOUT_SEC"] = "30.0"
        try:
            import trading.entry_exit.tasks as tasks
            tasks.RANKING_ENTRY_BUILD_TIMEOUT_SEC = 30.0
            tasks.RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC = 30.0
            tasks.RANKING_ENTRY_MAX_PENDING_PER_RUN = 4
        except Exception:
            pass
        return True
    except Exception:
        logger.exception("[RANKING ENTRY TIMEOUT FLOOR LAST] set caps failed")
        return False


def _watch() -> None:
    for i in range(45):
        ok = _set_caps()
        if i in (0, 44):
            logger.warning(
                "[RANKING ENTRY TIMEOUT FLOOR LAST] enforce %s i=%s ok=%s runtime=%s build=%s controller=%s hard=%s max_pending=%s",
                VERSION,
                i,
                ok,
                os.environ.get("RANKING_ENTRY_RUNTIME_BUDGET_SEC"),
                os.environ.get("RANKING_ENTRY_BUILD_TIMEOUT_SEC"),
                os.environ.get("RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC"),
                os.environ.get("RANKING_ENTRY_HARD_TIMEOUT_SEC"),
                os.environ.get("RANKING_ENTRY_MAX_PENDING_PER_RUN"),
            )
        time.sleep(1.0)


def install() -> bool:
    global _DONE
    with _LOCK:
        ok = _set_caps()
        if not _DONE:
            _DONE = True
            threading.Thread(target=_watch, name="ranking-entry-timeout-floor-last", daemon=True).start()
        logger.warning(
            "[RANKING ENTRY TIMEOUT FLOOR LAST] installed %s ok=%s runtime=%s build=%s controller=%s hard=%s max_pending=%s",
            VERSION,
            ok,
            os.environ.get("RANKING_ENTRY_RUNTIME_BUDGET_SEC"),
            os.environ.get("RANKING_ENTRY_BUILD_TIMEOUT_SEC"),
            os.environ.get("RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC"),
            os.environ.get("RANKING_ENTRY_HARD_TIMEOUT_SEC"),
            os.environ.get("RANKING_ENTRY_MAX_PENDING_PER_RUN"),
        )
        return bool(ok)


try:
    install()
except Exception:
    logger.exception("[RANKING ENTRY TIMEOUT FLOOR LAST] auto install failed")

__all__ = ["install"]
