from __future__ import annotations

import logging
import os
import threading
import time

logger = logging.getLogger(__name__)
_INSTALLED = False
_STOP = False


def _apply_once() -> bool:
    """
    USERCUSTOMIZE 側で後勝ち適用されるため、ここで短すぎる既定値を使うと
    ranking_entry_controller_timeout_patch の 150/180/120 秒設定を潰してしまう。

    旧既定:
      runtime=25 / build=30 / controller=30
    新既定:
      runtime=150 / build=180 / controller=120
    """
    os.environ["RANKING_ENTRY_RUNTIME_BUDGET_SEC"] = os.getenv("RANKING_ENTRY_FAST_RUNTIME_BUDGET_SEC", "150")
    os.environ["RANKING_ENTRY_BUILD_TIMEOUT_SEC"] = os.getenv("RANKING_ENTRY_FAST_BUILD_TIMEOUT_SEC", "180")
    os.environ["RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC"] = os.getenv("RANKING_ENTRY_FAST_CONTROLLER_TIMEOUT_SEC", "120")
    os.environ["RANKING_ENTRY_MAX_PENDING_PER_RUN"] = os.getenv("RANKING_ENTRY_FAST_MAX_PENDING_PER_RUN", "1")
    os.environ.setdefault("RANKING_ENTRY_TIMEOUT_COOLDOWN_SEC", "90")
    os.environ.setdefault("RANKING_ENTRY_TIMEOUT_COOLDOWN_MAX_SEC", "300")
    try:
        import trading.entry_exit.tasks as tasks
        tasks.RANKING_ENTRY_BUILD_TIMEOUT_SEC = float(os.environ["RANKING_ENTRY_BUILD_TIMEOUT_SEC"])
        tasks.RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC = float(os.environ["RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC"])
    except Exception:
        return False
    return True


def _watch_loop() -> None:
    # 他patchが後から変更しても、起動後しばらく最後に現在の安全値を維持する。
    for i in range(120):
        try:
            ok = _apply_once()
            if i in (0, 1, 5, 15, 30, 60, 119):
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
            "[RANKING ENTRY FAST BUDGET OVERRIDE] installed v2 ok=%s runtime_budget=%s build_timeout=%s controller_timeout=%s max_pending=%s watcher=True",
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
