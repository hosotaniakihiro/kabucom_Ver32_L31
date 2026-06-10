from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)
_DONE = False
_INSTALL_LOCK = threading.Lock()


VERSION = "v3"


def _env_bool(name: str, default: bool = False) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _floor_float(name: str, floor: float, default: float | None = None, cap: float | None = None) -> float:
    x = _env_float(name, float(default if default is not None else floor))
    if x < floor:
        x = floor
    if cap is not None and x > cap:
        x = cap
    return float(x)


def _force_final_caps() -> float:
    """Last-defense cap after all other ranking runtime patches.

    V2 accidentally clamped ranking entry back to 15/18/12s after the fast-budget
    and stuck-pending patches widened it.  That made ranking candidates time out
    before pending_add and caused created=0 even after lunch reopen.

    V3 is intentionally a *floor*, not a clamp-down: keep at least 25s runtime
    budget, 30s build/controller timeouts, and a 35s outer hard timeout so the
    scheduler slot is still released if the worker truly hangs.
    """
    timeout_sec = _floor_float("RANKING_ENTRY_HARD_TIMEOUT_SEC", 35.0, 35.0, 55.0)
    runtime_budget = _floor_float("RANKING_ENTRY_RUNTIME_BUDGET_SEC", 25.0, 25.0, timeout_sec)
    runtime_warn = _floor_float("RANKING_ENTRY_RUNTIME_WARN_SEC", 25.0, 25.0, timeout_sec)
    runtime_stale = _floor_float("RANKING_ENTRY_RUNTIME_STALE_SEC", 35.0, 35.0, 60.0)
    build_timeout = _floor_float("RANKING_ENTRY_BUILD_TIMEOUT_SEC", 30.0, 30.0, timeout_sec)
    controller_timeout = _floor_float("RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC", 30.0, 30.0, timeout_sec)
    max_pending = max(4, int(_env_float("RANKING_ENTRY_MAX_PENDING_PER_RUN", 4)))
    max_pending = min(max_pending, 4)

    os.environ["RANKING_ENTRY_HARD_TIMEOUT_SEC"] = str(timeout_sec)
    os.environ["RANKING_ENTRY_RUNTIME_BUDGET_SEC"] = str(runtime_budget)
    os.environ["RANKING_ENTRY_RUNTIME_WARN_SEC"] = str(runtime_warn)
    os.environ["RANKING_ENTRY_RUNTIME_STALE_SEC"] = str(runtime_stale)
    os.environ["RANKING_ENTRY_BUILD_TIMEOUT_SEC"] = str(build_timeout)
    os.environ["RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC"] = str(controller_timeout)
    os.environ["RANKING_ENTRY_FAST_RUNTIME_BUDGET_SEC"] = str(runtime_budget)
    os.environ["RANKING_ENTRY_FAST_BUILD_TIMEOUT_SEC"] = str(build_timeout)
    os.environ["RANKING_ENTRY_FAST_CONTROLLER_TIMEOUT_SEC"] = str(controller_timeout)
    os.environ["RANKING_ENTRY_FAST_MAX_PENDING_PER_RUN"] = str(max_pending)
    os.environ["RANKING_ENTRY_MAX_PENDING_PER_RUN"] = str(max_pending)
    return timeout_sec


def _patch_once() -> bool:
    try:
        if not _env_bool("RANKING_ENTRY_HARD_TIMEOUT_ENABLED", True):
            logger.warning("[RANKING ENTRY HARD TIMEOUT] disabled by env")
            return False

        timeout_sec = _force_final_caps()

        import trading.entry_exit.tasks as tasks

        cur = getattr(tasks, "_run_ranking_entry_safe", None)
        if not callable(cur):
            return False
        if getattr(cur, "_ranking_entry_hard_timeout_v3", False):
            _force_final_caps()
            return True

        orig = getattr(cur, "_original_hard_timeout", cur)

        def patched(*args, **kwargs):
            timeout_sec_inner = _force_final_caps()
            result: dict[str, Any] = {"done": False, "ret": 0, "exc": None}
            started = time.time()

            def worker() -> None:
                try:
                    result["ret"] = orig(*args, **kwargs)
                except Exception as e:  # keep scheduler alive
                    result["exc"] = e
                    logger.exception("[RANKING ENTRY HARD TIMEOUT] worker exception")
                finally:
                    result["done"] = True

            th = threading.Thread(target=worker, name="ranking-entry-hard-timeout-worker", daemon=True)
            th.start()
            th.join(timeout_sec_inner)

            elapsed = time.time() - started
            if th.is_alive():
                logger.warning(
                    "[RANKING ENTRY HARD TIMEOUT] timeout elapsed=%.1fs >= %.1fs; return 0 to release scheduler slot. worker continues in daemon thread.",
                    elapsed,
                    timeout_sec_inner,
                )
                try:
                    import core.startup.ranking_stuck_pending_prune_patch as stuck
                    fn = getattr(stuck, "_mark_and_prune_stuck_ranking_pending", None)
                    if callable(fn):
                        fn(reason="RANKING_ENTRY_HARD_TIMEOUT_35S")
                except Exception:
                    logger.debug("[RANKING ENTRY HARD TIMEOUT] prune on timeout failed", exc_info=True)
                return 0

            if result.get("exc") is not None:
                return 0
            logger.info("[RANKING ENTRY HARD TIMEOUT] completed elapsed=%.1fs ret=%s", elapsed, result.get("ret"))
            return result.get("ret", 0)

        patched._ranking_entry_hard_timeout_v1 = True  # type: ignore[attr-defined]
        patched._ranking_entry_hard_timeout_v2 = True  # type: ignore[attr-defined]
        patched._ranking_entry_hard_timeout_v3 = True  # type: ignore[attr-defined]
        patched._original_hard_timeout = orig  # type: ignore[attr-defined]
        patched._original = getattr(cur, "_original", orig)  # type: ignore[attr-defined]
        tasks._run_ranking_entry_safe = patched
        logger.warning(
            "[RANKING ENTRY HARD TIMEOUT] patched tasks._run_ranking_entry_safe %s timeout=%ss final_caps=runtime:%s build:%s controller:%s max_pending:%s",
            VERSION,
            timeout_sec,
            os.environ.get("RANKING_ENTRY_RUNTIME_BUDGET_SEC"),
            os.environ.get("RANKING_ENTRY_BUILD_TIMEOUT_SEC"),
            os.environ.get("RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC"),
            os.environ.get("RANKING_ENTRY_MAX_PENDING_PER_RUN"),
        )
        return True
    except Exception:
        logger.exception("[RANKING ENTRY HARD TIMEOUT] patch failed")
        return False


def install() -> bool:
    global _DONE
    with _INSTALL_LOCK:
        if _DONE:
            return _patch_once()
        ok = _patch_once()
        _DONE = True
        logger.warning(
            "[RANKING ENTRY HARD TIMEOUT] installed %s ok=%s timeout=%ss runtime=%s build=%s controller=%s max_pending=%s",
            VERSION,
            ok,
            os.environ.get("RANKING_ENTRY_HARD_TIMEOUT_SEC"),
            os.environ.get("RANKING_ENTRY_RUNTIME_BUDGET_SEC"),
            os.environ.get("RANKING_ENTRY_BUILD_TIMEOUT_SEC"),
            os.environ.get("RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC"),
            os.environ.get("RANKING_ENTRY_MAX_PENDING_PER_RUN"),
        )
        return bool(ok)


try:
    install()
except Exception:
    logger.exception("[RANKING ENTRY HARD TIMEOUT] auto install failed")

__all__ = ["install"]
