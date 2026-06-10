from __future__ import annotations

import datetime as dt
import logging
import os
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)
_DONE = False
_SCHEDULER_STALE_PATCHED = False
_TASK_STALE_PATCHED = False
_COMPANION_PATCHED = False
_WATCHER_STARTED = False
_LAST_PATCH_LOG_TS = 0.0

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


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(v))
    except Exception:
        return int(default)


def _ensure_min_env_float(name: str, minimum: float) -> None:
    try:
        cur = _env_float(name, minimum)
        if cur < minimum:
            os.environ[name] = str(float(minimum))
            logger.warning("[RANKING ENTRY MARKET HOURS SKIP] env raised %s %.1f->%.1f", name, cur, minimum)
    except Exception:
        os.environ[name] = str(float(minimum))


def _in_session(now=None):
    now = now or dt.datetime.now()
    t = now.time()
    return (dt.time(9, 0) <= t <= dt.time(11, 30)) or (dt.time(12, 30) <= t <= dt.time(15, 30))


def _rate_limited_log(message: str, *args: Any) -> None:
    global _LAST_PATCH_LOG_TS
    now = time.time()
    interval = max(3.0, _env_float("RANKING_ENTRY_MARKET_PATCH_LOG_INTERVAL_SEC", 60.0))
    if now - _LAST_PATCH_LOG_TS >= interval:
        _LAST_PATCH_LOG_TS = now
        logger.warning(message, *args)


def _install_companion_patches() -> bool:
    """
    token互換と ranking DB empty fallback を連鎖 install する。
    V1.4: 成功後は二度と呼び直さない。
    """
    global _COMPANION_PATCHED
    if _COMPANION_PATCHED:
        return True
    ok_any = False
    try:
        from core.startup import kabu_api_token_runtime_patch as token_patch
        ok = bool(token_patch.install())
        ok_any = ok_any or ok
    except Exception:
        logger.debug("[RANKING ENTRY MARKET HOURS SKIP] companion token patch skipped", exc_info=True)

    try:
        from core.startup import ranking_entry_push_fallback_patch as push_fb
        ok = bool(push_fb.install())
        ok_any = ok_any or ok
    except Exception:
        logger.debug("[RANKING ENTRY MARKET HOURS SKIP] companion push fallback patch skipped", exc_info=True)

    if ok_any:
        logger.warning("[RANKING ENTRY MARKET HOURS SKIP] companion patches installed token/push_fallback ok_any=%s", ok_any)
    _COMPANION_PATCHED = bool(ok_any)
    return bool(ok_any)


def _run_with_watchdog(orig) -> Any:
    if not _env_bool("RANKING_ENTRY_WATCHDOG_ENABLED", True):
        return orig()

    timeout_sec = max(30.0, _env_float("RANKING_ENTRY_WATCHDOG_TIMEOUT_SEC", 30.0))
    result: dict[str, Any] = {"done": False, "ret": None, "error": None}

    def _target() -> None:
        try:
            result["ret"] = orig()
        except Exception as e:  # noqa: BLE001
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
            "ranking entry still blocked after timeout; worker is daemon and next tick may retry",
        )
        return 0

    if result.get("error") is not None:
        logger.exception("[RANKING ENTRY WATCHDOG] original ranking entry failed elapsed=%.3fs", elapsed, exc_info=result["error"])
        return 0

    logger.info("[RANKING ENTRY WATCHDOG] completed elapsed=%.3fs ret=%s", elapsed, result.get("ret"))
    return result.get("ret")


def _entry_stale_timeout_for_key(key: str) -> float:
    base = _env_float("ENTRY_SCHEDULER_STALE_RUNNING_CLEAR_SEC", 90.0)
    if "tonosama_entry" in key:
        return _env_float("TONOSAMA_ENTRY_SCHEDULER_STALE_SEC", base)
    if "ranking_entry" in key:
        return _env_float("RANKING_ENTRY_SCHEDULER_STALE_SEC", max(30.0, base))
    if "entry" in key:
        return base
    return 0.0


def _clear_task_running_if_stale(source: str, *, force: bool = False) -> bool:
    source_u = str(source or "").upper()
    try:
        import trading.entry_exit.tasks as tasks
    except Exception:
        logger.debug("[ENTRY TASK STALE CLEAR] tasks import failed source=%s", source_u, exc_info=True)
        return False
    if "TONOSAMA" in source_u:
        running_name = "_TONOSAMA_ENTRY_RUNNING"
        started_name = "_TONOSAMA_ENTRY_STARTED_AT"
        cooldown_name = "_TONOSAMA_ENTRY_COOLDOWN_UNTIL"
        orphan_name = "_TONOSAMA_ENTRY_ORPHAN_THREAD"
        timeout = _env_float("TONOSAMA_ENTRY_TASK_STALE_SEC", _env_float("TONOSAMA_ENTRY_SCHEDULER_STALE_SEC", 90.0))
    elif "RANKING" in source_u:
        running_name = "_RANKING_ENTRY_RUNNING"
        started_name = "_RANKING_ENTRY_STARTED_AT"
        cooldown_name = "_RANKING_ENTRY_COOLDOWN_UNTIL"
        orphan_name = None
        timeout = _env_float("RANKING_ENTRY_TASK_STALE_SEC", _env_float("RANKING_ENTRY_SCHEDULER_STALE_SEC", 30.0))
    else:
        return False

    try:
        lock = getattr(tasks, "_TONOSAMA_ENTRY_LOCK", None) if "TONOSAMA" in source_u else getattr(tasks, "_RANKING_ENTRY_LOCK", None)
        cm = lock if lock is not None else threading.RLock()
        with cm:
            running = bool(getattr(tasks, running_name, False))
            started_at = getattr(tasks, started_name, None)
            orphan = getattr(tasks, orphan_name, None) if orphan_name else None
            orphan_alive = bool(orphan is not None and getattr(orphan, "is_alive", lambda: False)())
            elapsed = None
            if isinstance(started_at, dt.datetime):
                elapsed = max(0.0, (dt.datetime.now() - started_at).total_seconds())
            should_clear = bool(force)
            if running and elapsed is not None and elapsed >= timeout:
                should_clear = True
            if orphan_alive and _env_bool("TONOSAMA_ENTRY_CLEAR_ORPHAN_THREAD_BLOCK", True) and elapsed is not None and elapsed >= timeout:
                should_clear = True

            if not should_clear:
                return False

            setattr(tasks, running_name, False)
            setattr(tasks, started_name, None)
            setattr(tasks, cooldown_name, None)
            if orphan_name:
                setattr(tasks, orphan_name, None)
            logger.warning(
                "[ENTRY TASK STALE CLEAR] cleared source=%s running_name=%s elapsed=%s timeout=%.3fs force=%s orphan_alive=%s",
                source_u,
                running_name,
                None if elapsed is None else round(float(elapsed), 3),
                timeout,
                force,
                orphan_alive,
            )
            return True
    except Exception:
        logger.exception("[ENTRY TASK STALE CLEAR] failed source=%s", source_u)
        return False


def _install_task_stale_running_clear() -> bool:
    global _TASK_STALE_PATCHED
    if _TASK_STALE_PATCHED:
        return True
    if not _env_bool("ENTRY_TASK_STALE_RUNNING_CLEAR_ENABLED", True):
        return False
    _clear_task_running_if_stale("TONOSAMA")
    _clear_task_running_if_stale("RANKING")
    _TASK_STALE_PATCHED = True
    logger.warning(
        "[ENTRY TASK STALE CLEAR] installed enabled=%s tonosama_task_timeout=%.1fs ranking_task_timeout=%.1fs",
        _env_bool("ENTRY_TASK_STALE_RUNNING_CLEAR_ENABLED", True),
        _env_float("TONOSAMA_ENTRY_TASK_STALE_SEC", _env_float("TONOSAMA_ENTRY_SCHEDULER_STALE_SEC", 90.0)),
        _env_float("RANKING_ENTRY_TASK_STALE_SEC", _env_float("RANKING_ENTRY_SCHEDULER_STALE_SEC", 30.0)),
    )
    return True


def _install_scheduler_stale_running_clear() -> bool:
    global _SCHEDULER_STALE_PATCHED
    if _SCHEDULER_STALE_PATCHED:
        return True
    if not _env_bool("ENTRY_SCHEDULER_STALE_RUNNING_CLEAR_ENABLED", True):
        return False

    try:
        import core.startup.schedule_loop as sl
        orig = getattr(sl, "_is_job_running", None)
        if not callable(orig):
            logger.warning("[ENTRY SCHEDULER STALE CLEAR] schedule_loop._is_job_running missing")
            return False
        if getattr(orig, "_entry_scheduler_stale_clear_v2", False):
            _SCHEDULER_STALE_PATCHED = True
            return True

        def patched_is_job_running(key: str) -> bool:
            try:
                key_s = str(key)
                timeout_sec = _entry_stale_timeout_for_key(key_s)
                if timeout_sec > 0:
                    lock = getattr(sl, "_RUNNING_JOBS_LOCK", None)
                    running = getattr(sl, "_RUNNING_JOBS", None)
                    if lock is not None and isinstance(running, dict):
                        with lock:
                            meta = running.get(key_s)
                            started_at = meta.get("started_at") if isinstance(meta, dict) else None
                            elapsed = 0.0
                            if isinstance(started_at, dt.datetime):
                                elapsed = max(0.0, (dt.datetime.now() - started_at).total_seconds())
                            if meta and elapsed >= timeout_sec:
                                running.pop(key_s, None)
                                if "tonosama_entry" in key_s:
                                    _clear_task_running_if_stale("TONOSAMA", force=True)
                                elif "ranking_entry" in key_s:
                                    _clear_task_running_if_stale("RANKING", force=True)
                                logger.warning(
                                    "[ENTRY SCHEDULER STALE CLEAR] cleared stale running key=%s elapsed=%.3fs timeout=%.3fs meta=%s",
                                    key_s,
                                    elapsed,
                                    timeout_sec,
                                    meta,
                                )
                                return False
            except Exception:
                logger.exception("[ENTRY SCHEDULER STALE CLEAR] check failed key=%s", key)
            return bool(orig(key))

        patched_is_job_running._entry_scheduler_stale_clear_v1 = True  # type: ignore[attr-defined]
        patched_is_job_running._entry_scheduler_stale_clear_v2 = True  # type: ignore[attr-defined]
        patched_is_job_running._original = orig  # type: ignore[attr-defined]
        sl._is_job_running = patched_is_job_running
        _SCHEDULER_STALE_PATCHED = True
        logger.warning(
            "[ENTRY SCHEDULER STALE CLEAR] installed enabled=%s default_timeout=%.1fs tonosama_timeout=%.1fs ranking_timeout=%.1fs task_clear=True",
            _env_bool("ENTRY_SCHEDULER_STALE_RUNNING_CLEAR_ENABLED", True),
            _env_float("ENTRY_SCHEDULER_STALE_RUNNING_CLEAR_SEC", 90.0),
            _env_float("TONOSAMA_ENTRY_SCHEDULER_STALE_SEC", _env_float("ENTRY_SCHEDULER_STALE_RUNNING_CLEAR_SEC", 90.0)),
            _env_float("RANKING_ENTRY_SCHEDULER_STALE_SEC", max(30.0, _env_float("ENTRY_SCHEDULER_STALE_RUNNING_CLEAR_SEC", 90.0))),
        )
        return True
    except Exception:
        logger.exception("[ENTRY SCHEDULER STALE CLEAR] install failed")
        return False


def _is_stuck_pending_wrapper(fn: Any) -> bool:
    return bool(callable(fn) and any(getattr(fn, f"_ranking_stuck_pending_prune_v{i}", False) for i in range(1, 10)))


def _patch_once():
    try:
        companion_ok = _install_companion_patches()
        import trading.entry_exit.tasks as tasks

        task_ok = _install_task_stale_running_clear()
        _clear_task_running_if_stale("TONOSAMA")
        _clear_task_running_if_stale("RANKING")

        cur = getattr(tasks, "_run_ranking_entry_safe", None)
        if not callable(cur):
            return False

        # V1.4: ranking_stuck_pending_prune が既にtimebox/overlapを持つ場合は、
        # ここでさらに watchdog wrapper を重ねない。属性だけ付けて相互再wrapを止める。
        if _is_stuck_pending_wrapper(cur):
            cur._ranking_market_hours_skip_v2_watchdog = True  # type: ignore[attr-defined]
            cur._ranking_market_hours_skip_v14_passthrough = True  # type: ignore[attr-defined]
            _rate_limited_log(
                "[RANKING ENTRY MARKET HOURS SKIP] detected ranking_stuck_pending wrapper -> no rewrap v1.4 companion=%s task_clear=%s",
                companion_ok,
                task_ok,
            )
            return True

        if getattr(cur, "_ranking_market_hours_skip_v14", False) or getattr(cur, "_ranking_market_hours_skip_v2_watchdog", False):
            return True

        orig = getattr(cur, "_original", cur)

        def patched():
            now = dt.datetime.now()
            if not _in_session(now):
                logger.warning("[RANKING ENTRY MARKET HOURS SKIP] skip outside session now=%s", now.strftime("%Y-%m-%d %H:%M:%S"))
                return 0
            _clear_task_running_if_stale("RANKING")
            return _run_with_watchdog(orig)

        patched._ranking_market_hours_skip_v14 = True  # type: ignore[attr-defined]
        patched._ranking_market_hours_skip_v2_watchdog = True  # type: ignore[attr-defined]
        patched._original = orig  # type: ignore[attr-defined]
        tasks._run_ranking_entry_safe = patched
        _rate_limited_log(
            "[RANKING ENTRY MARKET HOURS SKIP] patched _run_ranking_entry_safe v1.4 watchdog=%s timeout=%.1fs task_stale_clear=%s companion=%s",
            _env_bool("RANKING_ENTRY_WATCHDOG_ENABLED", True),
            _env_float("RANKING_ENTRY_WATCHDOG_TIMEOUT_SEC", 30.0),
            task_ok,
            companion_ok,
        )
        return True
    except Exception:
        logger.exception("[RANKING ENTRY MARKET HOURS SKIP] patch failed")
        return False


def _watch():
    loops = max(1, min(_env_int("RANKING_ENTRY_MARKET_HOURS_ENFORCE_LOOPS", 4), 12))
    sleep_sec = max(1.0, min(_env_float("RANKING_ENTRY_MARKET_HOURS_ENFORCE_SLEEP_SEC", 3.0), 10.0))
    for i in range(loops):
        ok = _patch_once()
        stale_ok = _install_scheduler_stale_running_clear()
        task_ok = _install_task_stale_running_clear()
        if i in (0, loops - 1):
            logger.warning("[RANKING ENTRY MARKET HOURS SKIP] enforce v1.4 i=%s/%s ok=%s stale_clear_ok=%s task_clear_ok=%s companion_ok=%s", i, loops, ok, stale_ok, task_ok, _COMPANION_PATCHED)
        time.sleep(sleep_sec)


def install():
    global _DONE, _WATCHER_STARTED
    os.environ.setdefault("ENTRY_TASK_STALE_RUNNING_CLEAR_ENABLED", "1")
    os.environ.setdefault("ENTRY_SCHEDULER_STALE_RUNNING_CLEAR_ENABLED", "1")
    os.environ.setdefault("ENTRY_SCHEDULER_STALE_RUNNING_CLEAR_SEC", "90")
    os.environ.setdefault("TONOSAMA_ENTRY_SCHEDULER_STALE_SEC", "60")
    os.environ.setdefault("TONOSAMA_ENTRY_TASK_STALE_SEC", "60")
    os.environ.setdefault("RANKING_ENTRY_SCHEDULER_STALE_SEC", "30")
    os.environ.setdefault("RANKING_ENTRY_TASK_STALE_SEC", "30")
    os.environ.setdefault("RANKING_ENTRY_WATCHDOG_TIMEOUT_SEC", "30")
    os.environ.setdefault("RANKING_ENTRY_PUSH_SUMMARY_FALLBACK_ENABLED", "1")
    os.environ.setdefault("RANKING_PUSH_FALLBACK_MAX_ROWS", "80")

    if _DONE:
        return True

    companion_ok = _install_companion_patches()
    stale_ok = _install_scheduler_stale_running_clear()
    task_ok = _install_task_stale_running_clear()
    ok = _patch_once()
    if not _WATCHER_STARTED:
        threading.Thread(target=_watch, name="ranking-entry-market-hours-skip", daemon=True).start()
        _WATCHER_STARTED = True
    _DONE = True
    logger.warning("[RANKING ENTRY MARKET HOURS SKIP] installed v1.4 ok=%s stale_clear_ok=%s task_clear_ok=%s companion_ok=%s watcher=%s", ok, stale_ok, task_ok, companion_ok, _WATCHER_STARTED)
    return True


try:
    install()
except Exception:
    logger.exception("[RANKING ENTRY MARKET HOURS SKIP] auto install failed")


__all__ = ["install"]
