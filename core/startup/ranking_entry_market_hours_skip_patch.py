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


def _entry_stale_timeout_for_key(key: str) -> float:
    """entry系 schedule job の running 残りを解除する秒数。"""
    base = _env_float("ENTRY_SCHEDULER_STALE_RUNNING_CLEAR_SEC", 90.0)
    if "tonosama_entry" in key:
        return _env_float("TONOSAMA_ENTRY_SCHEDULER_STALE_SEC", base)
    if "ranking_entry" in key:
        return _env_float("RANKING_ENTRY_SCHEDULER_STALE_SEC", max(75.0, base))
    if "entry" in key:
        return base
    return 0.0


def _clear_task_running_if_stale(source: str, *, force: bool = False) -> bool:
    """trading.entry_exit.tasks 内部の *_RUNNING フラグ残りを解除する。

    schedule_loop 側の running は解除できても、tasks.py 内部の
    _TONOSAMA_ENTRY_RUNNING / _RANKING_ENTRY_RUNNING が True のままだと
    `previous_still_running` で再び止まる。ログの 300秒超 stuck をここで復旧する。
    """
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
        timeout = _env_float("RANKING_ENTRY_TASK_STALE_SEC", _env_float("RANKING_ENTRY_SCHEDULER_STALE_SEC", 90.0))
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
        _clear_task_running_if_stale("TONOSAMA")
        _clear_task_running_if_stale("RANKING")
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
        _env_float("RANKING_ENTRY_TASK_STALE_SEC", _env_float("RANKING_ENTRY_SCHEDULER_STALE_SEC", 90.0)),
    )
    return True


def _install_scheduler_stale_running_clear() -> bool:
    """
    schedule_loop の _is_job_running を外側から補強する。

    実運用ログで `tags:entry,tonosama_entry` が 200秒以上 running のまま残り、
    以後 `previous_still_running` で entry が発火しない状態を確認したため、
    entry系だけ stale running を解除する。
    """
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
                                try:
                                    stats_set = getattr(sl, "_stats_set", None)
                                    stats_inc = getattr(sl, "_stats_inc", None)
                                    if callable(stats_inc):
                                        stats_inc(key_s, "stale_running_cleared_count", 1)
                                    if callable(stats_set):
                                        stats_set(
                                            key_s,
                                            last_stale_clear_at=str(dt.datetime.now()),
                                            last_stale_clear_elapsed_sec=round(float(elapsed), 3),
                                            last_stale_clear_timeout_sec=round(float(timeout_sec), 3),
                                        )
                                except Exception:
                                    pass
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
            _env_float("RANKING_ENTRY_SCHEDULER_STALE_SEC", max(75.0, _env_float("ENTRY_SCHEDULER_STALE_RUNNING_CLEAR_SEC", 90.0))),
        )
        return True
    except Exception:
        logger.exception("[ENTRY SCHEDULER STALE CLEAR] install failed")
        return False


def _patch_once():
    try:
        import trading.entry_exit.tasks as tasks

        task_ok = _install_task_stale_running_clear()
        _clear_task_running_if_stale("TONOSAMA")
        _clear_task_running_if_stale("RANKING")

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
            _clear_task_running_if_stale("RANKING")
            return _run_with_watchdog(orig)

        patched._ranking_market_hours_skip_v2_watchdog = True  # type: ignore[attr-defined]
        patched._original = orig  # type: ignore[attr-defined]
        tasks._run_ranking_entry_safe = patched
        logger.warning(
            "[RANKING ENTRY MARKET HOURS SKIP] patched _run_ranking_entry_safe watchdog=%s timeout=%.1fs task_stale_clear=%s",
            _env_bool("RANKING_ENTRY_WATCHDOG_ENABLED", True),
            _env_float("RANKING_ENTRY_WATCHDOG_TIMEOUT_SEC", 55.0),
            task_ok,
        )
        return True
    except Exception:
        logger.exception("[RANKING ENTRY MARKET HOURS SKIP] patch failed")
        return False


def _watch():
    for i in range(240):
        ok = _patch_once()
        stale_ok = _install_scheduler_stale_running_clear()
        task_ok = _install_task_stale_running_clear()
        if i in (0, 1, 5, 15, 30, 60, 120, 239):
            logger.warning("[RANKING ENTRY MARKET HOURS SKIP] enforce ok=%s stale_clear_ok=%s task_clear_ok=%s", ok, stale_ok, task_ok)
        time.sleep(0.5)


def install():
    global _DONE
    os.environ.setdefault("ENTRY_TASK_STALE_RUNNING_CLEAR_ENABLED", "1")
    os.environ.setdefault("ENTRY_SCHEDULER_STALE_RUNNING_CLEAR_ENABLED", "1")
    os.environ.setdefault("ENTRY_SCHEDULER_STALE_RUNNING_CLEAR_SEC", "90")
    os.environ.setdefault("TONOSAMA_ENTRY_SCHEDULER_STALE_SEC", "60")
    os.environ.setdefault("RANKING_ENTRY_SCHEDULER_STALE_SEC", "75")
    os.environ.setdefault("TONOSAMA_ENTRY_TASK_STALE_SEC", "60")
    os.environ.setdefault("RANKING_ENTRY_TASK_STALE_SEC", "75")
    stale_ok = _install_scheduler_stale_running_clear()
    task_ok = _install_task_stale_running_clear()
    if _DONE:
        return bool(_patch_once() and stale_ok and task_ok)
    ok = _patch_once()
    threading.Thread(target=_watch, name="ranking-entry-market-hours-skip", daemon=True).start()
    _DONE = True
    logger.warning("[RANKING ENTRY MARKET HOURS SKIP] installed ok=%s stale_clear_ok=%s task_clear_ok=%s watcher=True", ok, stale_ok, task_ok)
    return True


try:
    install()
except Exception:
    logger.exception("[RANKING ENTRY MARKET HOURS SKIP] auto install failed")


__all__ = ["install"]