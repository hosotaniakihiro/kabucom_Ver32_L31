# ============================================================
# File   : core/startup/summary_parallel_intervals_runtime_patch.py
# Version: Ver11-MAIN-TICK-TIMEOUT-CAP
# ------------------------------------------------------------
# 1分・3分・5分サマリーを並列実行する runtime patch。
#
# Ver10 Fix:
#   ✔ CPU高止まり対策として、既定で 1m/3m/5m 全足強制をしない。
#   ✔ main.py は親tickを詰まらせないが、長足BGも既定では増やさない。
#   ✔ workers/bg_workers 既定を 1 にしてスレッド増殖を抑える。
# Ver11 Fix:
#   ✔ main.py / entry-only process では 90秒 timeout を引き継がず、
#     25秒で親tickを返す。summary_parent_tick 80秒化を防ぐ。
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
_BG_RUNNING_KEYS: set[str] = set()
_EXECUTOR: ThreadPoolExecutor | None = None
_BG_EXECUTOR: ThreadPoolExecutor | None = None


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


def _setdefault_env(name: str, value: str) -> None:
    try:
        cur = os.getenv(name)
        if cur is None or str(cur).strip() == "":
            os.environ[name] = str(value)
            logger.warning("[SUMMARY PARALLEL] env default set %s=%s", name, value)
    except Exception:
        pass


def _force_env(name: str, value: str, *, reason: str) -> None:
    try:
        old = os.getenv(name)
        if str(old or "").strip() != str(value):
            os.environ[name] = str(value)
            logger.warning("[SUMMARY PARALLEL] env forced %s=%s old=%s reason=%s", name, value, old, reason)
    except Exception:
        pass


def _is_main_entry_only_process() -> bool:
    try:
        if _env_bool("AUTOSTOCK_MAIN_DATABASE_PROCESS", False):
            return False
        if _env_bool("AUTOSTOCK_DATA_COLLECTORS_PROCESS", False):
            return False
        role = str(os.getenv("SUMMARY_DB_WRITER_ROLE") or "").strip().lower()
        return _env_bool("SUMMARY_MAIN_ENTRY_ONLY", False) or role in {"entry_only", "main_entry_only", "read_only", "no_save"}
    except Exception:
        return False


def _main_tick_timeout_cap_enabled() -> bool:
    # main.py は発注・PUSH監視を止めないことを優先する。長い集計は main_database.py 側。
    return _is_main_entry_only_process() and not _env_bool("SUMMARY_MAIN_ALLOW_LONG_TICK", False)


def _ensure_timeout_min() -> None:
    try:
        if _main_tick_timeout_cap_enabled():
            cap = _env_float("SUMMARY_MAIN_TICK_TIMEOUT_CAP_SEC", 25.0)
            cur_raw = os.getenv("SUMMARY_PARALLEL_INTERVAL_TIMEOUT_SEC")
            cur = float(cur_raw) if cur_raw is not None and str(cur_raw).strip() != "" else cap
            if cur > cap:
                os.environ["SUMMARY_PARALLEL_INTERVAL_TIMEOUT_SEC"] = str(int(cap) if float(cap).is_integer() else cap)
                logger.warning(
                    "[SUMMARY PARALLEL] timeout capped old=%s new=%s reason=main_entry_tick_latency",
                    cur_raw,
                    os.environ.get("SUMMARY_PARALLEL_INTERVAL_TIMEOUT_SEC"),
                )
            min_raw = os.getenv("SUMMARY_PARALLEL_TIMEOUT_MIN_SEC")
            min_sec = float(min_raw) if min_raw is not None and str(min_raw).strip() != "" else cap
            if min_sec > cap:
                os.environ["SUMMARY_PARALLEL_TIMEOUT_MIN_SEC"] = str(int(cap) if float(cap).is_integer() else cap)
                logger.warning(
                    "[SUMMARY PARALLEL] timeout min capped old=%s new=%s reason=main_entry_tick_latency",
                    min_raw,
                    os.environ.get("SUMMARY_PARALLEL_TIMEOUT_MIN_SEC"),
                )
            return

        min_sec = _env_float("SUMMARY_PARALLEL_TIMEOUT_MIN_SEC", 30.0)
        cur_raw = os.getenv("SUMMARY_PARALLEL_INTERVAL_TIMEOUT_SEC")
        cur = float(cur_raw) if cur_raw is not None and str(cur_raw).strip() != "" else min_sec
        if cur < min_sec:
            os.environ["SUMMARY_PARALLEL_INTERVAL_TIMEOUT_SEC"] = str(int(min_sec) if float(min_sec).is_integer() else min_sec)
            logger.warning("[SUMMARY PARALLEL] timeout raised old=%s new=%s reason=min_timeout_for_summary", cur_raw, os.environ.get("SUMMARY_PARALLEL_INTERVAL_TIMEOUT_SEC"))
    except Exception:
        os.environ["SUMMARY_PARALLEL_INTERVAL_TIMEOUT_SEC"] = "25" if _main_tick_timeout_cap_enabled() else "30"
        logger.warning("[SUMMARY PARALLEL] timeout fallback set %s", os.environ.get("SUMMARY_PARALLEL_INTERVAL_TIMEOUT_SEC"))


def _executor() -> ThreadPoolExecutor:
    global _EXECUTOR
    workers = _env_int("SUMMARY_PARALLEL_INTERVAL_WORKERS", 1)
    if _EXECUTOR is None:
        _EXECUTOR = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="summary-parallel-interval")
    return _EXECUTOR


def _bg_executor() -> ThreadPoolExecutor:
    global _BG_EXECUTOR
    workers = _env_int("SUMMARY_PUSH_BG_INTERVAL_WORKERS", 1)
    if _BG_EXECUTOR is None:
        _BG_EXECUTOR = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="summary-push-bg-interval")
    return _BG_EXECUTOR


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


def _is_market_session(now: dt.datetime) -> bool:
    try:
        from scheduler_jobs.summary.time_utils import is_market_session
        return bool(is_market_session(now))
    except Exception:
        return True


def _base_time_locked_targets(now: dt.datetime, in_session: bool) -> list[int]:
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

    clean = sorted({int(x) for x in targets if int(x) in {1, 3, 5}})
    return clean or [1]


def _force_all_targets_enabled() -> bool:
    return _env_bool("SUMMARY_PARALLEL_FORCE_1_3_5", False)


def _push_all_intervals_enabled() -> bool:
    return _env_bool("SUMMARY_PUSH_DISPLAY_ALL_INTERVALS", False)


def _push_bg_all_intervals_enabled() -> bool:
    return _env_bool("SUMMARY_PUSH_BG_ALL_INTERVALS", False)


def _push_bg_long_intervals_enabled() -> bool:
    return _env_bool("SUMMARY_PUSH_BG_LONG_INTERVALS", False)


def _resolve_targets(now: dt.datetime, in_session: bool) -> tuple[list[int], list[int]]:
    base = _base_time_locked_targets(now, in_session)
    force_all = _force_all_targets_enabled()

    ranking_targets = list(base)
    if in_session and force_all:
        ranking_targets = [1, 3, 5]

    push_targets = [1, 3, 5] if _push_all_intervals_enabled() else list(base)
    push_targets = sorted({int(x) for x in push_targets if int(x) in {1, 3, 5}}) or [1]
    ranking_targets = sorted({int(x) for x in ranking_targets if int(x) in {1, 3, 5}}) or [1]
    return push_targets, ranking_targets


def _split_push_wait_and_bg(push_targets: list[int], *, in_session: bool) -> tuple[list[int], list[int]]:
    targets = sorted({int(x) for x in push_targets if int(x) in {1, 3, 5}})

    if _is_main_entry_only_process() and _env_bool("SUMMARY_MAIN_BG_PUSH_ENABLED", False):
        if (not bool(in_session)) and _env_bool("SUMMARY_PUSH_SKIP_BG_WHEN_OUT_OF_SESSION", True):
            return [], []
        return [], targets

    if _push_bg_all_intervals_enabled():
        return [], targets

    if _push_bg_long_intervals_enabled():
        wait = [x for x in targets if int(x) == 1]
        bg = [x for x in targets if int(x) in (3, 5)]
        return (wait or [1]), bg

    return targets, []


def _job_one_source(*, source: str, interval: int, now: dt.datetime, display: bool, run_entry: bool) -> tuple[str, int, pd.DataFrame]:
    t0 = time.perf_counter()
    try:
        from scheduler_jobs.summary.runner_core import job_summary, job_ranking_summary
        if source == "push":
            logger.info("[SUMMARY PARALLEL] job start source=push interval=%s now=%s display=%s run_entry=%s", interval, now, display, run_entry)
            df = job_summary(int(interval), display=display, now=now, run_entry=run_entry)
        else:
            logger.info("[SUMMARY PARALLEL] job start source=ranking interval=%s now=%s display=%s run_entry=%s", interval, now, display, run_entry)
            df = job_ranking_summary(int(interval), display=display, now=now, run_entry=False)
        rows = len(df) if isinstance(df, pd.DataFrame) else 0
        logger.warning("[SUMMARY PARALLEL] job done source=%s interval=%s rows=%s latest_dt=%s elapsed=%.3fs", source, interval, rows, _latest_dt_str(df) if isinstance(df, pd.DataFrame) else None, time.perf_counter() - t0)
        return source, interval, df if isinstance(df, pd.DataFrame) else pd.DataFrame()
    except Exception as e:
        logger.exception("[SUMMARY PARALLEL] job failed source=%s interval=%s now=%s err=%s elapsed=%.3fs", source, interval, now, e, time.perf_counter() - t0)
        return source, interval, pd.DataFrame()


def _submit_bg_push_interval(*, interval: int, now: dt.datetime, display: bool, run_entry: bool) -> None:
    bg_key = f"{now.strftime('%Y%m%d%H%M')}:push:{int(interval)}"
    with _RUNNING_LOCK:
        if bg_key in _BG_RUNNING_KEYS:
            logger.warning("[SUMMARY PARALLEL] bg push skipped reason=already_running key=%s", bg_key)
            return
        _BG_RUNNING_KEYS.add(bg_key)

    def _task() -> None:
        try:
            logger.warning("[SUMMARY PARALLEL] bg push start key=%s interval=%s now=%s display=%s run_entry=%s", bg_key, interval, now, display, run_entry)
            _job_one_source(source="push", interval=int(interval), now=now, display=display, run_entry=run_entry)
        finally:
            with _RUNNING_LOCK:
                _BG_RUNNING_KEYS.discard(bg_key)
            logger.warning("[SUMMARY PARALLEL] bg push done key=%s interval=%s now=%s", bg_key, interval, now)

    _bg_executor().submit(_task)
    logger.warning("[SUMMARY PARALLEL] bg push submitted key=%s interval=%s now=%s", bg_key, interval, now)


def _patched_run_time_locked_summary_jobs(*, now: Optional[dt.datetime] = None, run_push: bool = True, run_ranking: bool = True, display: bool = True, run_entry: bool = True) -> dict[str, dict[int, pd.DataFrame]]:
    if not _env_bool("SUMMARY_PARALLEL_INTERVALS_ENABLED", True):
        if callable(_ORIG_TIME_LOCKED):
            return _ORIG_TIME_LOCKED(now=now, run_push=run_push, run_ranking=run_ranking, display=display, run_entry=run_entry)
        return {"push": {}, "ranking": {}}

    _ensure_timeout_min()
    n = (now or _now_naive()).replace(microsecond=0)
    in_session = _is_market_session(n)
    push_targets, ranking_targets = _resolve_targets(n, in_session)
    wait_push_targets, bg_push_targets = _split_push_wait_and_bg(push_targets, in_session=in_session)
    out: dict[str, dict[int, pd.DataFrame]] = {"push": {}, "ranking": {}}

    key = n.strftime("%Y%m%d%H%M")
    with _RUNNING_LOCK:
        if key in _RUNNING_KEYS:
            logger.warning("[SUMMARY PARALLEL] skipped reason=previous_same_tick_running key=%s push_targets=%s wait_push_targets=%s bg_push_targets=%s ranking_targets=%s", key, push_targets, wait_push_targets, bg_push_targets, ranking_targets)
            return out
        _RUNNING_KEYS.add(key)

    t0 = time.perf_counter()
    futures = []
    timeout = _env_float("SUMMARY_PARALLEL_INTERVAL_TIMEOUT_SEC", 30.0)
    try:
        logger.warning(
            "[SUMMARY PARALLEL] tick start now=%s push_targets=%s wait_push_targets=%s bg_push_targets=%s ranking_targets=%s run_push=%s run_ranking=%s display=%s run_entry=%s in_session=%s workers=%s bg_workers=%s timeout=%.1f force_1_3_5=%s push_all_intervals=%s push_bg_all=%s push_bg_long=%s main_entry_only=%s",
            n, push_targets, wait_push_targets, bg_push_targets, ranking_targets, run_push, run_ranking, display, run_entry,
            in_session, _env_int("SUMMARY_PARALLEL_INTERVAL_WORKERS", 1), _env_int("SUMMARY_PUSH_BG_INTERVAL_WORKERS", 1), timeout,
            _force_all_targets_enabled(), _push_all_intervals_enabled(), _push_bg_all_intervals_enabled(),
            _push_bg_long_intervals_enabled(), _is_main_entry_only_process(),
        )
        ex = _executor()

        if run_push:
            for interval in wait_push_targets:
                futures.append(ex.submit(_job_one_source, source="push", interval=int(interval), now=n, display=display, run_entry=bool(run_entry) and bool(in_session)))
            for interval in bg_push_targets:
                _submit_bg_push_interval(interval=int(interval), now=n, display=display, run_entry=bool(run_entry) and bool(in_session))

        if run_ranking and in_session and _env_bool("SUMMARY_PARALLEL_RANKING_ENABLED", True):
            for interval in ranking_targets:
                futures.append(ex.submit(_job_one_source, source="ranking", interval=int(interval), now=n, display=display, run_entry=False))
        elif run_ranking and not in_session:
            logger.info("[SUMMARY PARALLEL] ranking skipped reason=closed_market_or_lunch targets=%s", ranking_targets)
        elif run_ranking:
            logger.info("[SUMMARY PARALLEL] ranking skipped reason=parallel_ranking_disabled targets=%s", ranking_targets)

        if not futures:
            logger.warning("[SUMMARY PARALLEL] tick no wait futures now=%s bg_push_targets=%s elapsed=%.3fs", n, bg_push_targets, time.perf_counter() - t0)
            return out

        done_count = 0
        try:
            for fut in as_completed(futures, timeout=timeout):
                source, interval, df = fut.result()
                out.setdefault(source, {})[int(interval)] = df
                done_count += 1
        except FuturesTimeoutError:
            logger.error("[SUMMARY PARALLEL] tick timeout now=%s timeout=%.1fs done=%s total=%s wait_push_targets=%s bg_push_targets=%s ranking_targets=%s", n, timeout, done_count, len(futures), wait_push_targets, bg_push_targets, ranking_targets)
            for fut in futures:
                try:
                    fut.cancel()
                except Exception:
                    pass

        logger.warning("[SUMMARY PARALLEL] tick done now=%s push_targets=%s wait_push_targets=%s bg_push_targets=%s ranking_targets=%s push_done=%s ranking_done=%s elapsed=%.3fs", n, push_targets, wait_push_targets, bg_push_targets, ranking_targets, sorted(out.get("push", {}).keys()), sorted(out.get("ranking", {}).keys()), time.perf_counter() - t0)
        return out
    finally:
        with _RUNNING_LOCK:
            _RUNNING_KEYS.discard(key)


def install() -> bool:
    global _INSTALLED, _ORIG_TIME_LOCKED

    # 軽量既定: 環境変数で明示された時だけ全足/長足BGを有効化する。
    _setdefault_env("SUMMARY_PARALLEL_FORCE_1_3_5", "0")
    _setdefault_env("SUMMARY_PUSH_BG_ALL_INTERVALS", "0")
    _setdefault_env("SUMMARY_PUSH_BG_LONG_INTERVALS", "0")
    _setdefault_env("SUMMARY_PUSH_DISPLAY_ALL_INTERVALS", "0")
    _setdefault_env("SUMMARY_PARALLEL_TIMEOUT_MIN_SEC", "30")
    _setdefault_env("SUMMARY_PARALLEL_INTERVAL_WORKERS", "1")
    _setdefault_env("SUMMARY_PUSH_BG_INTERVAL_WORKERS", "1")

    if _INSTALLED:
        _ensure_timeout_min()
        return True

    _ensure_timeout_min()

    try:
        import scheduler_jobs.summary.time_locked_runner as tlr
        import scheduler_jobs.summary.runners as runners
        import scheduler_jobs.summary.scheduler as scheduler
        cur = getattr(tlr, "run_time_locked_summary_jobs", None)
        if getattr(cur, "_summary_parallel_intervals_v11", False):
            _INSTALLED = True
            return True
        _ORIG_TIME_LOCKED = cur
        _patched_run_time_locked_summary_jobs._summary_parallel_intervals_v11 = True  # type: ignore[attr-defined]
        tlr.run_time_locked_summary_jobs = _patched_run_time_locked_summary_jobs
        runners.run_time_locked_summary_jobs = _patched_run_time_locked_summary_jobs
        scheduler.run_time_locked_summary_jobs = _patched_run_time_locked_summary_jobs
        _INSTALLED = True
        logger.warning(
            "[SUMMARY PARALLEL] installed v11 enabled=%s workers=%s bg_workers=%s timeout=%.1f ranking_parallel=%s force_1_3_5=%s push_all_intervals=%s push_bg_all=%s push_bg_long=%s main_entry_only=%s min_timeout=%s cap_enabled=%s",
            _env_bool("SUMMARY_PARALLEL_INTERVALS_ENABLED", True),
            _env_int("SUMMARY_PARALLEL_INTERVAL_WORKERS", 1),
            _env_int("SUMMARY_PUSH_BG_INTERVAL_WORKERS", 1),
            _env_float("SUMMARY_PARALLEL_INTERVAL_TIMEOUT_SEC", 30.0),
            _env_bool("SUMMARY_PARALLEL_RANKING_ENABLED", True),
            _force_all_targets_enabled(),
            _push_all_intervals_enabled(),
            _push_bg_all_intervals_enabled(),
            _push_bg_long_intervals_enabled(),
            _is_main_entry_only_process(),
            os.getenv("SUMMARY_PARALLEL_TIMEOUT_MIN_SEC"),
            _main_tick_timeout_cap_enabled(),
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
