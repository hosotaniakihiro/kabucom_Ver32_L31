from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)
_DONE = False
_INSTALL_LOCK = threading.Lock()


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


def _force_final_caps() -> float:
    """Last-defense cap after all other ranking runtime patches.

    Some startup/usercustomize patches intentionally widen ranking_entry to 25-30s.
    This module is installed after those patches in sitecustomize, so it clamps the
    scheduler-visible hard timeout back to 15s and keeps the environment aligned.
    """
    timeout_sec = max(5.0, min(_env_float("RANKING_ENTRY_HARD_TIMEOUT_SEC", 15.0), 15.0))
    os.environ["RANKING_ENTRY_HARD_TIMEOUT_SEC"] = str(timeout_sec)
    os.environ["RANKING_ENTRY_RUNTIME_BUDGET_SEC"] = str(min(_env_float("RANKING_ENTRY_RUNTIME_BUDGET_SEC", timeout_sec), timeout_sec))
    os.environ["RANKING_ENTRY_RUNTIME_WARN_SEC"] = str(min(_env_float("RANKING_ENTRY_RUNTIME_WARN_SEC", timeout_sec), timeout_sec))
    os.environ["RANKING_ENTRY_BUILD_TIMEOUT_SEC"] = str(min(_env_float("RANKING_ENTRY_BUILD_TIMEOUT_SEC", 18.0), 18.0))
    os.environ["RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC"] = str(min(_env_float("RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC", 12.0), 12.0))
    os.environ["RANKING_ENTRY_MAX_PENDING_PER_RUN"] = str(min(int(_env_float("RANKING_ENTRY_MAX_PENDING_PER_RUN", 3)), 3))
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
        if getattr(cur, "_ranking_entry_hard_timeout_v2", False):
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
                        fn(reason="RANKING_ENTRY_HARD_TIMEOUT_15S")
                except Exception:
                    logger.debug("[RANKING ENTRY HARD TIMEOUT] prune on timeout failed", exc_info=True)
                return 0

            if result.get("exc") is not None:
                return 0
            logger.info("[RANKING ENTRY HARD TIMEOUT] completed elapsed=%.1fs ret=%s", elapsed, result.get("ret"))
            return result.get("ret", 0)

        patched._ranking_entry_hard_timeout_v1 = True  # type: ignore[attr-defined]
        patched._ranking_entry_hard_timeout_v2 = True  # type: ignore[attr-defined]
        patched._original_hard_timeout = orig  # type: ignore[attr-defined]
        patched._original = getattr(cur, "_original", orig)  # type: ignore[attr-defined]
        tasks._run_ranking_entry_safe = patched
        logger.warning("[RANKING ENTRY HARD TIMEOUT] patched tasks._run_ranking_entry_safe v2 timeout=%ss final_caps=15s", timeout_sec)
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
        logger.warning("[RANKING ENTRY HARD TIMEOUT] installed v2 ok=%s timeout=15s", ok)
        return bool(ok)


try:
    install()
except Exception:
    logger.exception("[RANKING ENTRY HARD TIMEOUT] auto install failed")

__all__ = ["install"]
