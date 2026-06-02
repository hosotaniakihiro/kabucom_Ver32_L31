from __future__ import annotations

import logging
import os
import threading
import time

logger = logging.getLogger(__name__)
_INSTALLED = False


def _apply_once() -> bool:
    """
    ランキングENTRYはV5で軽量化済みのため、長時間timeoutへ戻さない。

    固定値:
      runtime=25秒
      build=30秒
      controller=30秒
      max_pending=1
    """
    os.environ["RANKING_ENTRY_RUNTIME_BUDGET_SEC"] = os.getenv("RANKING_ENTRY_FAST_RUNTIME_BUDGET_SEC", "25")
    os.environ["RANKING_ENTRY_BUILD_TIMEOUT_SEC"] = os.getenv("RANKING_ENTRY_FAST_BUILD_TIMEOUT_SEC", "30")
    os.environ["RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC"] = os.getenv("RANKING_ENTRY_FAST_CONTROLLER_TIMEOUT_SEC", "30")
    os.environ["RANKING_ENTRY_MAX_PENDING_PER_RUN"] = os.getenv("RANKING_ENTRY_FAST_MAX_PENDING_PER_RUN", "1")
    os.environ.setdefault("RANKING_ENTRY_TIMEOUT_COOLDOWN_SEC", "60")
    os.environ.setdefault("RANKING_ENTRY_TIMEOUT_COOLDOWN_MAX_SEC", "180")
    try:
        import trading.entry_exit.tasks as tasks
        tasks.RANKING_ENTRY_BUILD_TIMEOUT_SEC = float(os.environ["RANKING_ENTRY_BUILD_TIMEOUT_SEC"])
        tasks.RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC = float(os.environ["RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC"])
    except Exception:
        return False
    return True


def _watch_loop() -> None:
    # 後勝ちpatchが150秒へ戻しても、2分間は0.5秒ごとに25/30/30へ戻す。
    for i in range(240):
        try:
            ok = _apply_once()
            if i in (0, 1, 5, 15, 30, 60, 120, 180, 239):
                logger.warning(
                    "[RANKING ENTRY FAST BUDGET OVERRIDE] enforce ok=%s runtime_budget=%s build_timeout=%s controller_timeout=%s max_pending=%s",
                    ok,
                    os.environ.get("RANKING_ENTRY_RUNTIME_BUDGET_SEC"),
                    os.environ.get("RANKING_ENTRY_BUILD_TIMEOUT_SEC"),
                    os.environ.get("RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC"),
                    os.environ.get("RANKING_ENTRY_MAX_PENDING_PER_RUN"),
                )
        except Exception:
            logger.exception("[RANKING ENTRY FAST BUDGET OVERRIDE] enforce failed")
        time.sleep(0.5)


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        _apply_once()
        return True
    try:
        ok = _apply_once()
        threading.Thread(target=_watch_loop, name="ranking-entry-fast-budget-override", daemon=True).start()
        _INSTALLED = True
        logger.warning(
            "[RANKING ENTRY FAST BUDGET OVERRIDE] installed v3 ok=%s runtime_budget=%s build_timeout=%s controller_timeout=%s max_pending=%s watcher=True",
            ok,
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
