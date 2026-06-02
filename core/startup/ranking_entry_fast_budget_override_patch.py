from __future__ import annotations

import logging
import os
import threading
import time

logger = logging.getLogger(__name__)
_INSTALLED = False


def _float_env(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _clamp(v: float, lo: float, hi: float) -> float:
    try:
        x = float(v)
    except Exception:
        x = lo
    return max(float(lo), min(float(hi), x))


def _apply_once() -> bool:
    """
    ランキングENTRYを長時間化させない最終上書きpatch。

    目的:
      ranking_entry_controller_timeout_patch や外部ENVが
      runtime=150 / build=180 / controller=120 に戻しても、
      このpatchで 25 / 30 / 30 秒へ丸める。

    固定上限:
      runtime_budget <= 25秒
      build_timeout  <= 30秒
      controller_timeout <= 30秒
      max_pending = 1
    """
    raw_runtime = _float_env("RANKING_ENTRY_FAST_RUNTIME_BUDGET_SEC", 25.0)
    raw_build = _float_env("RANKING_ENTRY_FAST_BUILD_TIMEOUT_SEC", 30.0)
    raw_controller = _float_env("RANKING_ENTRY_FAST_CONTROLLER_TIMEOUT_SEC", 30.0)

    runtime = _clamp(raw_runtime, 10.0, 25.0)
    build = _clamp(raw_build, 15.0, 30.0)
    controller = _clamp(raw_controller, 15.0, 30.0)

    os.environ["RANKING_ENTRY_RUNTIME_BUDGET_SEC"] = str(runtime)
    os.environ["RANKING_ENTRY_BUILD_TIMEOUT_SEC"] = str(build)
    os.environ["RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC"] = str(controller)
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
    # 後勝ちpatchが150/180/120へ戻しても、起動後しばらく0.5秒ごとに25/30/30へ戻す。
    for i in range(240):
        try:
            ok = _apply_once()
            if i in (0, 1, 5, 15, 30, 60, 120, 180, 239):
                logger.warning(
                    "[RANKING ENTRY FAST BUDGET OVERRIDE] enforce ok=%s runtime_budget=%s build_timeout=%s controller_timeout=%s max_pending=%s cap=25/30/30",
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
            "[RANKING ENTRY FAST BUDGET OVERRIDE] installed v5 ok=%s runtime_budget=%s build_timeout=%s controller_timeout=%s max_pending=%s watcher=True cap=25/30/30",
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
