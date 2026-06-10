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


def _patch_once() -> bool:
    try:
        if not _env_bool("RANKING_ENTRY_HARD_TIMEOUT_ENABLED", True):
            logger.warning("[RANKING ENTRY HARD TIMEOUT] disabled by env")
            return False

        import trading.entry_exit.tasks as tasks

        cur = getattr(tasks, "_run_ranking_entry_safe", None)
        if not callable(cur):
            return False
        if getattr(cur, "_ranking_entry_hard_timeout_v1", False):
            return True

        orig = getattr(cur, "_original_hard_timeout", cur)

        def patched(*args, **kwargs):
            timeout_sec = max(5.0, _env_float("RANKING_ENTRY_HARD_TIMEOUT_SEC", 28.0))
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
            th.join(timeout_sec)

            elapsed = time.time() - started
            if th.is_alive():
                logger.warning(
                    "[RANKING ENTRY HARD TIMEOUT] timeout elapsed=%.1fs >= %.1fs; return 0 to release scheduler slot. worker continues in daemon thread.",
                    elapsed,
                    timeout_sec,
                )
                try:
                    # Best effort: if the stuck-pending patch is installed, mark/prune stale pending now.
                    import core.startup.ranking_stuck_pending_prune_patch as stuck
                    fn = getattr(stuck, "_mark_and_prune_stuck_ranking_pending", None)
                    if callable(fn):
                        fn(reason="RANKING_ENTRY_HARD_TIMEOUT")
                except Exception:
                    logger.debug("[RANKING ENTRY HARD TIMEOUT] prune on timeout failed", exc_info=True)
                return 0

            if result.get("exc") is not None:
                return 0
            logger.info("[RANKING ENTRY HARD TIMEOUT] completed elapsed=%.1fs ret=%s", elapsed, result.get("ret"))
            return result.get("ret", 0)

        patched._ranking_entry_hard_timeout_v1 = True  # type: ignore[attr-defined]
        patched._original_hard_timeout = orig  # type: ignore[attr-defined]
        # Preserve the generic _original chain where possible for later patches.
        patched._original = getattr(cur, "_original", orig)  # type: ignore[attr-defined]
        tasks._run_ranking_entry_safe = patched
        logger.warning("[RANKING ENTRY HARD TIMEOUT] patched tasks._run_ranking_entry_safe timeout=%ss", os.getenv("RANKING_ENTRY_HARD_TIMEOUT_SEC", "28"))
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
        logger.warning("[RANKING ENTRY HARD TIMEOUT] installed v1 ok=%s", ok)
        return bool(ok)


try:
    install()
except Exception:
    logger.exception("[RANKING ENTRY HARD TIMEOUT] auto install failed")

__all__ = ["install"]
