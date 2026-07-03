# ============================================================
# File   : core/startup/summary_parallel_executor_reset_patch.py
# Version: V1-MAIN-TICK-LOCAL-EXECUTOR-RESET
# ------------------------------------------------------------
# main.py の SUMMARY 1m tick が timeout した後、古い Future が
# ThreadPoolExecutor の single worker を占有し、次 tick が後ろに
# 並んで 50〜120 秒遅延する症状を防ぐ。
# ============================================================
from __future__ import annotations

import datetime as dt
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeoutError
from typing import Any, Optional

import pandas as pd

logger = logging.getLogger(__name__)
VERSION = "V1-MAIN-TICK-LOCAL-EXECUTOR-RESET"
_INSTALLED = False
_WATCHER_STARTED = False
_ORIG = None

_TRUE = {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
_FALSE = {"0", "false", "no", "n", "off", "ng", "disable", "disabled", ""}


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None:
            return bool(default)
        s = str(v).strip().lower()
        if s in _TRUE:
            return True
        if s in _FALSE:
            return False
    except Exception:
        pass
    return bool(default)


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return max(1, int(float(v)))
    except Exception:
        return int(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return max(1.0, float(v))
    except Exception:
        return float(default)


def _module():
    import core.startup.summary_parallel_intervals_runtime_patch as sp
    return sp


def _use_local_executor(sp: Any) -> bool:
    try:
        if not _env_bool("SUMMARY_PARALLEL_RESET_EXECUTOR_PER_TICK", True):
            return False
        fn = getattr(sp, "_main_tick_timeout_cap_enabled", None)
        return bool(fn()) if callable(fn) else True
    except Exception:
        return True


def _safe_latest(sp: Any, df: Any) -> str | None:
    try:
        fn = getattr(sp, "_latest_dt_str", None)
        if callable(fn):
            return fn(df)
    except Exception:
        pass
    return None


def _reset_global_executor(sp: Any, reason: str) -> None:
    try:
        ex = getattr(sp, "_EXECUTOR", None)
        if ex is not None:
            try:
                ex.shutdown(wait=False, cancel_futures=True)
            except TypeError:
                ex.shutdown(wait=False)
            except Exception:
                pass
        setattr(sp, "_EXECUTOR", None)
        logger.warning("[SUMMARY PARALLEL RESET] global executor reset reason=%s version=%s", reason, VERSION)
    except Exception:
        logger.exception("[SUMMARY PARALLEL RESET] global executor reset failed reason=%s", reason)


def _patched_run_time_locked_summary_jobs(*, now: Optional[dt.datetime] = None, run_push: bool = True, run_ranking: bool = True, display: bool = True, run_entry: bool = True) -> dict[str, dict[int, pd.DataFrame]]:
    sp = _module()
    if not sp._env_bool("SUMMARY_PARALLEL_INTERVALS_ENABLED", True):
        orig = getattr(sp, "_ORIG_TIME_LOCKED", None) or _ORIG
        if callable(orig):
            return orig(now=now, run_push=run_push, run_ranking=run_ranking, display=display, run_entry=run_entry)
        return {"push": {}, "ranking": {}}

    sp._ensure_timeout_min()
    n = (now or sp._now_naive()).replace(microsecond=0)
    in_session = bool(sp._is_market_session(n))
    push_targets, ranking_targets = sp._resolve_targets(n, in_session)
    wait_push_targets, bg_push_targets = sp._split_push_wait_and_bg(push_targets, in_session=in_session)
    out: dict[str, dict[int, pd.DataFrame]] = {"push": {}, "ranking": {}}

    key = n.strftime("%Y%m%d%H%M")
    with sp._RUNNING_LOCK:
        if key in sp._RUNNING_KEYS:
            logger.warning("[SUMMARY PARALLEL RESET] skipped reason=previous_same_tick_running key=%s", key)
            return out
        sp._RUNNING_KEYS.add(key)

    t0 = time.perf_counter()
    timeout = _env_float("SUMMARY_PARALLEL_INTERVAL_TIMEOUT_SEC", 25.0)
    local_ex = None
    ex = None
    futures = []
    use_local = _use_local_executor(sp)
    try:
        logger.warning(
            "[SUMMARY PARALLEL RESET] tick start now=%s push_targets=%s wait_push_targets=%s bg_push_targets=%s ranking_targets=%s run_push=%s run_ranking=%s display=%s run_entry=%s local_executor=%s timeout=%.1f version=%s",
            n, push_targets, wait_push_targets, bg_push_targets, ranking_targets, run_push, run_ranking, display, run_entry, use_local, timeout, VERSION,
        )
        if use_local:
            workers = _env_int("SUMMARY_PARALLEL_INTERVAL_WORKERS", 1)
            local_ex = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="summary-main-tick-local")
            ex = local_ex
        else:
            ex = sp._executor()

        if run_push:
            for interval in wait_push_targets:
                futures.append(ex.submit(sp._job_one_source, source="push", interval=int(interval), now=n, display=display, run_entry=bool(run_entry) and in_session))
            for interval in bg_push_targets:
                sp._submit_bg_push_interval(interval=int(interval), now=n, display=display, run_entry=bool(run_entry) and in_session)

        if run_ranking and in_session and sp._env_bool("SUMMARY_PARALLEL_RANKING_ENABLED", True):
            for interval in ranking_targets:
                futures.append(ex.submit(sp._job_one_source, source="ranking", interval=int(interval), now=n, display=display, run_entry=False))
        elif run_ranking:
            logger.info("[SUMMARY PARALLEL RESET] ranking skipped targets=%s in_session=%s enabled=%s", ranking_targets, in_session, sp._env_bool("SUMMARY_PARALLEL_RANKING_ENABLED", True))

        if not futures:
            logger.warning("[SUMMARY PARALLEL RESET] tick no wait futures now=%s elapsed=%.3fs", n, time.perf_counter() - t0)
            return out

        done_count = 0
        try:
            for fut in as_completed(futures, timeout=timeout):
                source, interval, df = fut.result()
                out.setdefault(source, {})[int(interval)] = df
                done_count += 1
        except FuturesTimeoutError:
            logger.error("[SUMMARY PARALLEL RESET] tick timeout now=%s timeout=%.1fs done=%s total=%s wait_push_targets=%s ranking_targets=%s local_executor=%s", n, timeout, done_count, len(futures), wait_push_targets, ranking_targets, use_local)
            for fut in futures:
                try:
                    fut.cancel()
                except Exception:
                    pass
            if not use_local:
                _reset_global_executor(sp, "timeout")

        logger.warning(
            "[SUMMARY PARALLEL RESET] tick done now=%s push_done=%s ranking_done=%s elapsed=%.3fs local_executor=%s",
            n, sorted(out.get("push", {}).keys()), sorted(out.get("ranking", {}).keys()), time.perf_counter() - t0, use_local,
        )
        return out
    finally:
        if local_ex is not None:
            try:
                local_ex.shutdown(wait=False, cancel_futures=True)
            except TypeError:
                local_ex.shutdown(wait=False)
            except Exception:
                pass
        with sp._RUNNING_LOCK:
            sp._RUNNING_KEYS.discard(key)


def _apply_once(reason: str) -> bool:
    global _ORIG
    try:
        sp = _module()
        import scheduler_jobs.summary.time_locked_runner as tlr
        import scheduler_jobs.summary.runners as runners
        import scheduler_jobs.summary.scheduler as scheduler
        cur = getattr(tlr, "run_time_locked_summary_jobs", None)
        if getattr(cur, "_summary_parallel_executor_reset_v1", False):
            return True
        if callable(cur) and not getattr(cur, "_summary_parallel_executor_reset_v1", False):
            _ORIG = cur
        _patched_run_time_locked_summary_jobs._summary_parallel_executor_reset_v1 = True  # type: ignore[attr-defined]
        tlr.run_time_locked_summary_jobs = _patched_run_time_locked_summary_jobs
        runners.run_time_locked_summary_jobs = _patched_run_time_locked_summary_jobs
        scheduler.run_time_locked_summary_jobs = _patched_run_time_locked_summary_jobs
        logger.warning("[SUMMARY PARALLEL RESET] applied reason=%s version=%s", reason, VERSION)
        return True
    except Exception:
        logger.exception("[SUMMARY PARALLEL RESET] apply failed reason=%s", reason)
        return False


def _watcher() -> None:
    for i in range(_env_int("SUMMARY_PARALLEL_RESET_WATCHER_LOOPS", 90)):
        try:
            _apply_once(f"watcher:{i}")
        except Exception:
            logger.exception("[SUMMARY PARALLEL RESET] watcher failed")
        time.sleep(max(1.0, _env_float("SUMMARY_PARALLEL_RESET_WATCHER_SEC", 2.0)))


def install() -> bool:
    global _INSTALLED, _WATCHER_STARTED
    if not _env_bool("SUMMARY_PARALLEL_EXECUTOR_RESET_PATCH_ENABLED", True):
        logger.warning("[SUMMARY PARALLEL RESET] disabled by env")
        return False
    ok = _apply_once("install")
    _INSTALLED = bool(ok)
    if not _WATCHER_STARTED and _env_bool("SUMMARY_PARALLEL_RESET_WATCHER_ENABLED", True):
        _WATCHER_STARTED = True
        threading.Thread(target=_watcher, name="summary-parallel-reset-watcher", daemon=True).start()
    logger.warning("[SUMMARY PARALLEL RESET] installed ok=%s version=%s", ok, VERSION)
    return bool(ok)


try:
    install()
except Exception:
    logger.exception("[SUMMARY PARALLEL RESET] auto install failed")


__all__ = ["install", "VERSION"]
