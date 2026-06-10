from __future__ import annotations

import datetime as dt
import logging
import os
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)
_DONE = False
_WATCH_STARTED = False


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _env_bool(name: str, default: bool = False) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _job_stale_sec(key: str, meta: dict[str, Any]) -> float:
    key_l = str(key or "").lower()
    tags = ",".join(str(x).lower() for x in (meta.get("tags") or []))
    func = str(meta.get("func") or "").lower()
    text = f"{key_l} {tags} {func}"

    if "ranking_entry" in text:
        return max(10.0, _env_float("SCHEDULE_LOOP_STALE_RANKING_ENTRY_SEC", 35.0))
    if "yahoo_complement" in text or "yahoo_wrapper" in text:
        return max(30.0, _env_float("SCHEDULE_LOOP_STALE_YAHOO_COMPLEMENT_SEC", 180.0))
    if "exit_loop_5s" in text or "exit" in tags:
        return max(10.0, _env_float("SCHEDULE_LOOP_STALE_EXIT_SEC", 25.0))
    if "summary_parent" in text:
        return max(30.0, _env_float("SCHEDULE_LOOP_STALE_SUMMARY_PARENT_SEC", 90.0))
    if "ranking_summary" in text:
        return max(30.0, _env_float("SCHEDULE_LOOP_STALE_RANKING_SUMMARY_SEC", 90.0))
    if "tonosama" in text:
        return max(20.0, _env_float("SCHEDULE_LOOP_STALE_TONOSAMA_SEC", 75.0))
    return max(60.0, _env_float("SCHEDULE_LOOP_STALE_DEFAULT_SEC", 180.0))


def _elapsed_sec(meta: dict[str, Any]) -> float:
    started_at = meta.get("started_at")
    try:
        if isinstance(started_at, dt.datetime):
            return max(0.0, (dt.datetime.now() - started_at).total_seconds())
        if isinstance(started_at, (int, float)):
            return max(0.0, time.time() - float(started_at))
        if isinstance(started_at, str) and started_at.strip():
            parsed = dt.datetime.fromisoformat(started_at.replace("Z", "+00:00").replace("+00:00", ""))
            return max(0.0, (dt.datetime.now() - parsed).total_seconds())
    except Exception:
        return 0.0
    return 0.0


def _release_stale_running_jobs(reason: str = "periodic") -> int:
    try:
        import core.startup.schedule_loop as sl

        lock = getattr(sl, "_RUNNING_JOBS_LOCK", None)
        running = getattr(sl, "_RUNNING_JOBS", None)
        stats_inc = getattr(sl, "_stats_inc", None)
        stats_set = getattr(sl, "_stats_set", None)
        if running is None:
            return 0

        removed: list[tuple[str, float, float, dict[str, Any]]] = []
        if lock is None:
            items = list(running.items())
            for key, meta in items:
                if not isinstance(meta, dict):
                    continue
                age = _elapsed_sec(meta)
                stale = _job_stale_sec(key, meta)
                if age >= stale:
                    removed.append((key, age, stale, dict(meta)))
                    running.pop(key, None)
        else:
            with lock:
                for key, meta in list(running.items()):
                    if not isinstance(meta, dict):
                        continue
                    age = _elapsed_sec(meta)
                    stale = _job_stale_sec(key, meta)
                    if age >= stale:
                        removed.append((key, age, stale, dict(meta)))
                        running.pop(key, None)

        for key, age, stale, meta in removed:
            try:
                if callable(stats_inc):
                    stats_inc(key, "stale_release_count", 1)
                if callable(stats_set):
                    stats_set(
                        key,
                        last_stale_release_at=str(dt.datetime.now()),
                        last_stale_release_age_sec=age,
                        last_stale_release_reason=reason,
                    )
            except Exception:
                pass
            logger.warning(
                "[SCHEDULE LOOP STALE RELEASE] released key=%s age=%.1fs stale=%.1fs reason=%s thread=%s func=%s tags=%s",
                key,
                age,
                stale,
                reason,
                meta.get("thread_name") or meta.get("thread"),
                meta.get("func"),
                meta.get("tags"),
            )
        return len(removed)
    except Exception:
        logger.exception("[SCHEDULE LOOP STALE RELEASE] release failed reason=%s", reason)
        return 0


def _patch_once() -> bool:
    try:
        import core.startup.schedule_loop as sl

        cur = getattr(sl, "_is_job_running", None)
        if not callable(cur):
            return False
        if getattr(cur, "_stale_release_v1", False):
            _release_stale_running_jobs(reason="enforce")
            return True

        orig = cur

        def _patched_is_job_running(key: str) -> bool:
            try:
                _release_stale_running_jobs(reason="precheck")
            except Exception:
                pass
            return bool(orig(key))

        _patched_is_job_running._stale_release_v1 = True  # type: ignore[attr-defined]
        _patched_is_job_running._original = orig  # type: ignore[attr-defined]
        sl._is_job_running = _patched_is_job_running
        logger.warning(
            "[SCHEDULE LOOP STALE RELEASE] patched _is_job_running v1 ranking=%.1fs yahoo=%.1fs exit=%.1fs default=%.1fs",
            _env_float("SCHEDULE_LOOP_STALE_RANKING_ENTRY_SEC", 35.0),
            _env_float("SCHEDULE_LOOP_STALE_YAHOO_COMPLEMENT_SEC", 180.0),
            _env_float("SCHEDULE_LOOP_STALE_EXIT_SEC", 25.0),
            _env_float("SCHEDULE_LOOP_STALE_DEFAULT_SEC", 180.0),
        )
        _release_stale_running_jobs(reason="install")
        return True
    except Exception:
        logger.exception("[SCHEDULE LOOP STALE RELEASE] patch failed")
        return False


def _watch() -> None:
    loops = int(max(1, min(_env_float("SCHEDULE_LOOP_STALE_RELEASE_WATCH_LOOPS", 240), 720)))
    sleep_sec = max(1.0, min(_env_float("SCHEDULE_LOOP_STALE_RELEASE_WATCH_SLEEP_SEC", 3.0), 15.0))
    for i in range(loops):
        ok = _patch_once()
        if i in (0, loops - 1) or i % 20 == 0:
            logger.warning("[SCHEDULE LOOP STALE RELEASE] enforce i=%s/%s ok=%s", i, loops, ok)
        time.sleep(sleep_sec)


def install() -> bool:
    global _DONE, _WATCH_STARTED
    if not _env_bool("SCHEDULE_LOOP_STALE_RELEASE_ENABLED", True):
        logger.warning("[SCHEDULE LOOP STALE RELEASE] disabled by env")
        return False
    ok = _patch_once()
    if not _WATCH_STARTED:
        threading.Thread(target=_watch, name="schedule-loop-stale-release-watch", daemon=True).start()
        _WATCH_STARTED = True
    _DONE = True
    logger.warning("[SCHEDULE LOOP STALE RELEASE] installed v1 ok=%s watcher=True", ok)
    return bool(ok)


try:
    install()
except Exception:
    logger.exception("[SCHEDULE LOOP STALE RELEASE] auto install failed")


__all__ = ["install"]
