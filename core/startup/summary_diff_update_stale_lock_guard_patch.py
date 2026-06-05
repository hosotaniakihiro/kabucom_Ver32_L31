from __future__ import annotations

import datetime as dt
import logging
import os
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)
_INSTALLED = False
_WATCHER_STARTED = False


def _env_bool(name: str, default: bool) -> bool:
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


def _clear_stale_locks_once(context: str = "manual") -> int:
    try:
        import trading.summary.summary_controller as sc
        guard = getattr(sc, "_SUMMARY_INFLIGHT_GUARD", None)
        inflight = getattr(sc, "_SUMMARY_INFLIGHT", None)
        if not isinstance(inflight, dict):
            return 0
        now = dt.datetime.now()
        stale_sec = _env_float("SUMMARY_DIFF_UPDATE_STALE_LOCK_SEC", 30.0)
        cleared = 0
        lock = guard if guard is not None else threading.Lock()
        with lock:
            for interval, meta in list(inflight.items()):
                try:
                    if not isinstance(meta, dict) or not bool(meta.get("running", False)):
                        continue
                    started_at = meta.get("started_at")
                    if not isinstance(started_at, dt.datetime):
                        continue
                    held = max(0.0, (now - started_at).total_seconds())
                    if held < stale_sec:
                        continue
                    inflight[int(interval)] = {
                        "running": False,
                        "started_at": None,
                        "tid": None,
                        "thread": None,
                        "stale_cleared_at": now,
                        "stale_context": context,
                        "held_sec": held,
                    }
                    cleared += 1
                    logger.error(
                        "[SUMMARY DIFF STALE LOCK GUARD] cleared stale diff_update lock interval=%s held=%.3fs threshold=%.3fs meta=%s context=%s",
                        interval, held, stale_sec, meta, context,
                    )
                except Exception:
                    logger.exception("[SUMMARY DIFF STALE LOCK GUARD] clear one failed interval=%s", interval)
        return cleared
    except Exception:
        logger.exception("[SUMMARY DIFF STALE LOCK GUARD] clear failed context=%s", context)
        return 0


def _patch_enter_interval() -> bool:
    try:
        import trading.summary.summary_controller as sc
        cur = getattr(sc, "_enter_interval", None)
        if not callable(cur):
            logger.warning("[SUMMARY DIFF STALE LOCK GUARD] _enter_interval unavailable")
            return False
        if getattr(cur, "_summary_diff_stale_lock_guard_v2", False):
            return True
        original = getattr(cur, "_original", cur)

        def _patched_enter_interval(interval: int) -> bool:
            try:
                _clear_stale_locks_once(context=f"before_enter:{interval}")
            except Exception:
                pass
            return original(interval)

        _patched_enter_interval._summary_diff_stale_lock_guard_v2 = True  # type: ignore[attr-defined]
        _patched_enter_interval._summary_diff_stale_lock_guard_v1 = True  # type: ignore[attr-defined]
        _patched_enter_interval._original = original  # type: ignore[attr-defined]
        sc._enter_interval = _patched_enter_interval
        return True
    except Exception:
        logger.exception("[SUMMARY DIFF STALE LOCK GUARD] patch enter failed")
        return False


def _watch_loop() -> None:
    while True:
        try:
            interval = max(2.0, _env_float("SUMMARY_DIFF_UPDATE_STALE_LOCK_WATCH_INTERVAL_SEC", 5.0))
            _clear_stale_locks_once(context="watcher")
            time.sleep(interval)
        except Exception:
            logger.exception("[SUMMARY DIFF STALE LOCK GUARD] watcher failed")
            time.sleep(5.0)


def install() -> bool:
    global _INSTALLED, _WATCHER_STARTED
    if not _env_bool("SUMMARY_DIFF_UPDATE_STALE_LOCK_GUARD_ENABLED", True):
        logger.warning("[SUMMARY DIFF STALE LOCK GUARD] disabled by env")
        return False
    # 1分足エントリー用なので90秒は長すぎる。30秒で解除する。
    old_stale = os.environ.get("SUMMARY_DIFF_UPDATE_STALE_LOCK_SEC")
    try:
        if old_stale is None or float(old_stale) > 30.0:
            os.environ["SUMMARY_DIFF_UPDATE_STALE_LOCK_SEC"] = "30"
    except Exception:
        os.environ["SUMMARY_DIFF_UPDATE_STALE_LOCK_SEC"] = "30"
    old_watch = os.environ.get("SUMMARY_DIFF_UPDATE_STALE_LOCK_WATCH_INTERVAL_SEC")
    try:
        if old_watch is None or float(old_watch) > 5.0:
            os.environ["SUMMARY_DIFF_UPDATE_STALE_LOCK_WATCH_INTERVAL_SEC"] = "5"
    except Exception:
        os.environ["SUMMARY_DIFF_UPDATE_STALE_LOCK_WATCH_INTERVAL_SEC"] = "5"
    ok = _patch_enter_interval()
    _clear_stale_locks_once(context="install")
    if ok and not _WATCHER_STARTED:
        _WATCHER_STARTED = True
        threading.Thread(target=_watch_loop, name="summary-diff-stale-lock-guard", daemon=True).start()
    _INSTALLED = bool(ok)
    logger.warning(
        "[SUMMARY DIFF STALE LOCK GUARD] installed v2 ok=%s stale_sec=%s watch_interval=%s watcher=%s",
        ok,
        os.environ.get("SUMMARY_DIFF_UPDATE_STALE_LOCK_SEC"),
        os.environ.get("SUMMARY_DIFF_UPDATE_STALE_LOCK_WATCH_INTERVAL_SEC"),
        _WATCHER_STARTED,
    )
    return bool(ok)

try:
    install()
except Exception:
    logger.exception("[SUMMARY DIFF STALE LOCK GUARD] auto install failed")

__all__ = ["install"]
