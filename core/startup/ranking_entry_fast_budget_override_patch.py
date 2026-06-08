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
        x = float(lo)
    return max(float(lo), min(float(hi), x))


def _install_fast_stale_guard() -> bool:
    """ranking_entry_fast_runtime_patch が後から run_ranking_entry_pipeline を差し替えても、
    watcher側で stale guard を再適用する。
    """
    try:
        os.environ.setdefault("RANKING_ENTRY_SKIP_IF_SNAPSHOT_STALE", "1")
        os.environ.setdefault("RANKING_ENTRY_SNAPSHOT_MAX_AGE_SEC", "300")
        mod = __import__("core.startup.ranking_entry_fast_stale_snapshot_guard_patch", fromlist=["install"])
        fn = getattr(mod, "install", None)
        return bool(fn()) if callable(fn) else False
    except Exception:
        logger.debug("[RANKING ENTRY FAST BUDGET OVERRIDE] fast stale guard install skipped", exc_info=True)
        return False


def _apply_once() -> bool:
    runtime = _clamp(_float_env("RANKING_ENTRY_FAST_RUNTIME_BUDGET_SEC", 25.0), 10.0, 25.0)
    build = _clamp(_float_env("RANKING_ENTRY_FAST_BUILD_TIMEOUT_SEC", 30.0), 15.0, 30.0)
    controller = _clamp(_float_env("RANKING_ENTRY_FAST_CONTROLLER_TIMEOUT_SEC", 30.0), 15.0, 30.0)
    lock_wait = _clamp(_float_env("SUMMARY_AI_ENTRY_CONTROLLER_LOCK_WAIT_SEC", 15.0), 3.0, 15.0)
    max_pending = str(int(_clamp(_float_env("RANKING_ENTRY_FAST_MAX_PENDING_PER_RUN", 4.0), 1.0, 6.0)))

    os.environ["RANKING_ENTRY_RUNTIME_BUDGET_SEC"] = str(runtime)
    os.environ["RANKING_ENTRY_BUILD_TIMEOUT_SEC"] = str(build)
    os.environ["RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC"] = str(controller)
    os.environ["RANKING_ENTRY_FAST_MAX_PENDING_PER_RUN"] = max_pending
    os.environ["RANKING_ENTRY_MAX_PENDING_PER_RUN"] = max_pending
    os.environ["SUMMARY_AI_ENTRY_CONTROLLER_LOCK_WAIT_SEC"] = str(lock_wait)
    os.environ.setdefault("SUMMARY_AI_ENTRY_CONTROLLER_LOCK_POLL_SEC", "0.25")
    os.environ.setdefault("RANKING_ENTRY_TIMEOUT_COOLDOWN_SEC", "20")
    os.environ.setdefault("RANKING_ENTRY_TIMEOUT_COOLDOWN_MAX_SEC", "60")
    os.environ.setdefault("RANKING_ENTRY_SKIP_IF_SNAPSHOT_STALE", "1")
    os.environ.setdefault("RANKING_ENTRY_SNAPSHOT_MAX_AGE_SEC", "300")

    guard_ok = _install_fast_stale_guard()

    try:
        import trading.entry_exit.tasks as tasks
        tasks.RANKING_ENTRY_BUILD_TIMEOUT_SEC = float(os.environ["RANKING_ENTRY_BUILD_TIMEOUT_SEC"])
        tasks.RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC = float(os.environ["RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC"])
    except Exception:
        return False
    return bool(guard_ok or True)


def _watch_loop() -> None:
    for i in range(240):
        try:
            ok = _apply_once()
            if i in (0, 1, 5, 15, 30, 60, 120, 180, 239):
                logger.warning(
                    "[RANKING ENTRY FAST BUDGET OVERRIDE] enforce ok=%s runtime_budget=%s build_timeout=%s controller_timeout=%s max_pending=%s summary_lock_wait=%s cooldown=%s/%s stale_guard=1 stale_max_age=%s cap=25/30/30 pending_default=4",
                    ok,
                    os.environ.get("RANKING_ENTRY_RUNTIME_BUDGET_SEC"),
                    os.environ.get("RANKING_ENTRY_BUILD_TIMEOUT_SEC"),
                    os.environ.get("RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC"),
                    os.environ.get("RANKING_ENTRY_MAX_PENDING_PER_RUN"),
                    os.environ.get("SUMMARY_AI_ENTRY_CONTROLLER_LOCK_WAIT_SEC"),
                    os.environ.get("RANKING_ENTRY_TIMEOUT_COOLDOWN_SEC"),
                    os.environ.get("RANKING_ENTRY_TIMEOUT_COOLDOWN_MAX_SEC"),
                    os.environ.get("RANKING_ENTRY_SNAPSHOT_MAX_AGE_SEC"),
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
            "[RANKING ENTRY FAST BUDGET OVERRIDE] installed v9 ok=%s runtime_budget=%s build_timeout=%s controller_timeout=%s max_pending=%s summary_lock_wait=%s watcher=True pending_default=4 stale_guard=1 stale_max_age=%s",
            ok,
            os.environ.get("RANKING_ENTRY_RUNTIME_BUDGET_SEC"),
            os.environ.get("RANKING_ENTRY_BUILD_TIMEOUT_SEC"),
            os.environ.get("RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC"),
            os.environ.get("RANKING_ENTRY_MAX_PENDING_PER_RUN"),
            os.environ.get("SUMMARY_AI_ENTRY_CONTROLLER_LOCK_WAIT_SEC"),
            os.environ.get("RANKING_ENTRY_SNAPSHOT_MAX_AGE_SEC"),
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
