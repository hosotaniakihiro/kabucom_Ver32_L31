from __future__ import annotations

import datetime as dt
import logging
import os
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)
_DONE = False

_TRUE = {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in _TRUE
    except Exception:
        return bool(default)


def _in_session(now=None):
    now = now or dt.datetime.now()
    t = now.time()
    return (dt.time(9, 0) <= t <= dt.time(11, 30)) or (dt.time(12, 30) <= t <= dt.time(15, 30))


def _run_with_watchdog(orig) -> Any:
    """
    ranking_entry が DB/API 待ちで固まると schedule_loop 側の running が残り、
    以後の 1分エントリーが全部 skip される。別 thread で orig() を走らせ、
    timeout 後は wrapper を返して schedule_loop の running を解除する。
    """
    if not _env_bool("RANKING_ENTRY_WATCHDOG_ENABLED", True):
        return orig()

    timeout_sec = max(10.0, _env_float("RANKING_ENTRY_WATCHDOG_TIMEOUT_SEC", 55.0))
    result: dict[str, Any] = {"done": False, "ret": None, "error": None}

    def _target() -> None:
        try:
            result["ret"] = orig()
        except Exception as e:  # noqa: BLE001 - keep original exception in wrapper log
            result["error"] = e
        finally:
            result["done"] = True

    th = threading.Thread(target=_target, name="ranking-entry-watchdog-worker", daemon=True)
    started = time.time()
    th.start()
    th.join(timeout_sec)
    elapsed = time.time() - started

    if th.is_alive():
        logger.error(
            "[RANKING ENTRY WATCHDOG] timeout -> release scheduler running state elapsed=%.3fs timeout=%.3fs thread_alive=%s hint=%s",
            elapsed,
            timeout_sec,
            True,
            "previous ranking_entry would block all later entry ticks; worker is daemon and next tick may retry",
        )
        return 0

    if result.get("error") is not None:
        logger.exception("[RANKING ENTRY WATCHDOG] original ranking entry failed elapsed=%.3fs", elapsed, exc_info=result["error"])
        return 0

    logger.info("[RANKING ENTRY WATCHDOG] completed elapsed=%.3fs ret=%s", elapsed, result.get("ret"))
    return result.get("ret")


def _patch_once():
    try:
        import trading.entry_exit.tasks as tasks

        cur = getattr(tasks, "_run_ranking_entry_safe", None)
        if not callable(cur):
            return False
        if getattr(cur, "_ranking_market_hours_skip_v2_watchdog", False):
            return True
        orig = getattr(cur, "_original", cur)

        def patched():
            now = dt.datetime.now()
            if not _in_session(now):
                logger.warning("[RANKING ENTRY MARKET HOURS SKIP] skip outside session now=%s", now.strftime("%Y-%m-%d %H:%M:%S"))
                return 0
            return _run_with_watchdog(orig)

        patched._ranking_market_hours_skip_v2_watchdog = True  # type: ignore[attr-defined]
        patched._original = orig  # type: ignore[attr-defined]
        tasks._run_ranking_entry_safe = patched
        logger.warning(
            "[RANKING ENTRY MARKET HOURS SKIP] patched _run_ranking_entry_safe watchdog=%s timeout=%.1fs",
            _env_bool("RANKING_ENTRY_WATCHDOG_ENABLED", True),
            _env_float("RANKING_ENTRY_WATCHDOG_TIMEOUT_SEC", 55.0),
        )
        return True
    except Exception:
        logger.exception("[RANKING ENTRY MARKET HOURS SKIP] patch failed")
        return False


def _watch():
    for i in range(240):
        ok = _patch_once()
        if i in (0, 1, 5, 15, 30, 60, 120, 239):
            logger.warning("[RANKING ENTRY MARKET HOURS SKIP] enforce ok=%s", ok)
        time.sleep(0.5)


def install():
    global _DONE
    if _DONE:
        return _patch_once()
    ok = _patch_once()
    threading.Thread(target=_watch, name="ranking-entry-market-hours-skip", daemon=True).start()
    _DONE = True
    logger.warning("[RANKING ENTRY MARKET HOURS SKIP] installed ok=%s watcher=True", ok)
    return True


try:
    install()
except Exception:
    logger.exception("[RANKING ENTRY MARKET HOURS SKIP] auto install failed")


__all__ = ["install"]
