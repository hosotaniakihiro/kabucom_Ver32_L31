# ============================================================
# File   : core/startup/summary_parallel_intervals_runtime_patch.py
# Version: Ver01-PARALLEL-1M-3M-5M-CALC
# ------------------------------------------------------------
# 1分・3分・5分サマリーを直列ではなく並列に実行する runtime patch。
#
# 目的:
#   - :00 / :03 / :05 などで 1m→3m→5m と順番待ちになり、
#     SUMMARY AI / エントリー投入が遅れる問題を緩和する
#   - 計算・表示・AI投入を interval 単位で並列化する
#
# 安全設計:
#   - DB保存は既存の save owner / safe_io / sqlite lock対策に任せる
#   - main.py 側は AUTOSTOCK_SUMMARY_SAVE_OWNER=database の場合、計算とAIだけでDB保存をskip可能
#   - SUMMARY AI実発注は既存 summary_ai_async_entry_patch により別workerへ投入済み
#   - 前回の同一tickがまだ走っている場合はスキップして詰まりを防止
#
# ENV:
#   SUMMARY_PARALLEL_INTERVALS_ENABLED=1
#   SUMMARY_PARALLEL_INTERVAL_WORKERS=3
#   SUMMARY_PARALLEL_INTERVAL_TIMEOUT_SEC=55
#   SUMMARY_PARALLEL_RANKING_ENABLED=1
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

_INSTALLED = False
_ORIG_TIME_LOCKED = None
_RUNNING_LOCK = threading.RLock()
_RUNNING_KEYS: set[str] = set()

_EXECUTOR: ThreadPoolExecutor | None = None


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        s = str(v).strip().lower()
        if s in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}:
            return True
        if s in {"0", "false", "no", "n", "off", "ng", "disable", "disabled"}:
            return False
        return bool(default)
    except Exception:
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


def _executor() -> ThreadPoolExecutor:
    global _EXECUTOR
    workers = _env_int("SUMMARY_PARALLEL_INTERVAL_WORKERS", 3)
    if _EXECUTOR is None:
        _EXECUTOR = ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="summary-parallel-interval",
        )
    return _EXECUTOR


def _now_naive() -> dt.datetime:
    try:
        from scheduler_jobs.summary.time_utils import now_naive
        return now_naive()
    except Exception:
        return dt.datetime.now()


def _latest_dt_str(df: Any) -> str | None:
    try:
        from scheduler_jobs.summary.display_prepare import latest_dt_str
        return latest_dt_str(df)
    except Exception:
        return None


def _resolve_target_intervals(now: dt.datetime, in_session: bool) -> list[int]:
    try:
        from scheduler_jobs.summary.time_utils import resolve_target_intervals
        targets = list(resolve_target_intervals(now) or [])
    except Exception:
        targets = []

    if not targets and not in_session:
        try:
            from scheduler_jobs.summary.time_locked_runner import closed_market_display_targets
            targets = list(closed_market_display_targets(now) or [])
        except Exception:
            targets = [1]
            try:
                if int(now.minute) % 3 == 0:
                    targets.append(3)
                if int(now.minute) % 5 == 0:
                    targets.append(5)
            except Exception:
                pass

    return sorted({int(x) for x in targets if int(x) in {1, 3, 5}})


def _is_market_session(now: dt.datetime) -> bool:
    try:
        from scheduler_jobs.summary.time_utils import is_market_session
        return bool(is_market_session(now))
    except Exception:
        return True


def _job_one_source(*, source: str, interval: int, now: dt.datetime, display: bool, run_entry: bool) -> tuple[str, int, pd.DataFrame]:
    t0 = time.perf_counter()
    try:
        from scheduler_jobs.summary.runner_core import job_summary, job_ranking_summary

        if source == "push":
            logger.info(
                "[SUMMARY PARALLEL] job start source=push interval=%s now=%s display=%s run_entry=%s",
                interval,
                now,
                display,
                run_entry,
            )
            df = job_summary(
                int(interval),
                display=display,
                now=now,
                run_entry=run_entry,
            )
        else:
            logger.info(
                "[SUMMARY PARALLEL] job start source=ranking interval=%s now=%s display=%s run_entry=%s",
                interval,
                now,
                display,
                run_entry,
            )
            df = job_ranking_summary(
                int(interval),
                display=display,
                now=now,
                run_entry=False,
            )

        rows = len(df) if isinstance(df, pd.DataFrame) else 0
        logger.warning(
            "[SUMMARY PARALLEL] job done source=%s interval=%s rows=%s latest_dt=%s elapsed=%.3fs",
            source,
            interval,
            rows,
            _latest_dt_str(df) if isinstance(df, pd.DataFrame) else None,
            time.perf_counter() - t0,
        )
        return source, interval, df if isinstance(df, pd.DataFrame) else pd.DataFrame()
    except Exception as e:
        logger.exception(
            "[SUMMARY PARALLEL] job failed source=%s interval=%s now=%s err=%s elapsed=%.3fs",
            source,
            interval,
            now,
            e,
            time.perf_counter() - t0,
        )
        return source, interval, pd.DataFrame()


def _patched_run_time_locked_summary_jobs(
    *,
    now: Optional[dt.datetime] = None,
    run_push: bool = True,
    run_ranking: bool = True,
    display: bool = True,
    run_entry: bool = True,
) -> dict[str, dict[int, pd.DataFrame]]:
    if not _env_bool("SUMMARY_PARALLEL_INTERVALS_ENABLED", True):
        if callable(_ORIG_TIME_LOCKED):
            return _ORIG_TIME_LOCKED(
                now=now,
                run_push=run_push,
                run_ranking=run_ranking,
                display=display,
                run_entry=run_entry,
            )
        return {"push": {}, "ranking": {}}

    n = (now or _now_naive()).replace(microsecond=0)
    in_session = _is_market_session(n)
    targets = _resolve_target_intervals(n, in_session)
    out: dict[str, dict[int, pd.DataFrame]] = {"push": {}, "ranking": {}}

    if not targets:
        logger.info(
            "[SUMMARY PARALLEL] skipped now=%s reason=no_target_intervals in_session=%s",
            n,
            in_session,
        )
        return out

    key = n.strftime("%Y%m%d%H%M")
    with _RUNNING_LOCK:
        if key in _RUNNING_KEYS:
            logger.warning("[SUMMARY PARALLEL] skipped reason=previous_same_tick_running key=%s targets=%s", key, targets)
            return out
        _RUNNING_KEYS.add(key)

    t0 = time.perf_counter()
    futures = []
    timeout = _env_float("SUMMARY_PARALLEL_INTERVAL_TIMEOUT_SEC", 55.0)

    try:
        logger.warning(
            "[SUMMARY PARALLEL] tick start now=%s targets=%s run_push=%s run_ranking=%s display=%s run_entry=%s in_session=%s workers=%s timeout=%.1f",
            n,
            targets,
            run_push,
            run_ranking,
            display,
            run_entry,
            in_session,
            _env_int("SUMMARY_PARALLEL_INTERVAL_WORKERS", 3),
            timeout,
        )

        ex = _executor()
        for interval in targets:
            if run_push:
                futures.append(ex.submit(
                    _job_one_source,
                    source="push",
                    interval=int(interval),
                    now=n,
                    display=display,
                    run_entry=bool(run_entry) and bool(in_session),
                ))
            if run_ranking and in_session and _env_bool("SUMMARY_PARALLEL_RANKING_ENABLED", True):
                futures.append(ex.submit(
                    _job_one_source,
                    source="ranking",
                    interval=int(interval),
                    now=n,
                    display=display,
                    run_entry=False,
                ))
            elif run_ranking and not in_session:
                logger.info("[SUMMARY PARALLEL] ranking skipped interval=%s reason=closed_market_or_lunch", interval)
            elif run_ranking:
                logger.info("[SUMMARY PARALLEL] ranking skipped interval=%s reason=parallel_ranking_disabled", interval)

        if not futures:
            return out

        done_count = 0
        try:
            for fut in as_completed(futures, timeout=timeout):
                source, interval, df = fut.result()
                out.setdefault(source, {})[int(interval)] = df
                done_count += 1
        except FuturesTimeoutError:
            logger.error(
                "[SUMMARY PARALLEL] tick timeout now=%s timeout=%.1fs done=%s total=%s",
                n,
                timeout,
                done_count,
                len(futures),
            )

        logger.warning(
            "[SUMMARY PARALLEL] tick done now=%s targets=%s push_done=%s ranking_done=%s elapsed=%.3fs",
            n,
            targets,
            sorted(out.get("push", {}).keys()),
            sorted(out.get("ranking", {}).keys()),
            time.perf_counter() - t0,
        )
        return out
    finally:
        with _RUNNING_LOCK:
            _RUNNING_KEYS.discard(key)


def install() -> bool:
    global _INSTALLED, _ORIG_TIME_LOCKED
    if _INSTALLED:
        return True

    try:
        import scheduler_jobs.summary.time_locked_runner as tlr
        import scheduler_jobs.summary.runners as runners
        import scheduler_jobs.summary.scheduler as scheduler

        cur = getattr(tlr, "run_time_locked_summary_jobs", None)
        if getattr(cur, "_summary_parallel_intervals_v1", False):
            _INSTALLED = True
            return True

        _ORIG_TIME_LOCKED = cur
        _patched_run_time_locked_summary_jobs._summary_parallel_intervals_v1 = True  # type: ignore[attr-defined]

        tlr.run_time_locked_summary_jobs = _patched_run_time_locked_summary_jobs
        runners.run_time_locked_summary_jobs = _patched_run_time_locked_summary_jobs
        # scheduler.py は from runners import run_time_locked_summary_jobs 済みなので参照も差し替える
        scheduler.run_time_locked_summary_jobs = _patched_run_time_locked_summary_jobs

        _INSTALLED = True
        logger.warning(
            "[SUMMARY PARALLEL] installed enabled=%s workers=%s timeout=%.1f ranking_parallel=%s",
            _env_bool("SUMMARY_PARALLEL_INTERVALS_ENABLED", True),
            _env_int("SUMMARY_PARALLEL_INTERVAL_WORKERS", 3),
            _env_float("SUMMARY_PARALLEL_INTERVAL_TIMEOUT_SEC", 55.0),
            _env_bool("SUMMARY_PARALLEL_RANKING_ENABLED", True),
        )
        return True
    except Exception as e:
        logger.exception("[SUMMARY PARALLEL] install failed err=%s", e)
        return False


try:
    install()
except Exception as e:
    logger.exception("[SUMMARY PARALLEL] auto install failed err=%s", e)

__all__ = ["install"]
