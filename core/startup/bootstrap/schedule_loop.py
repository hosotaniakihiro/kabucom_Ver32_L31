# ============================================================
# File   : core/startup/schedule_loop.py
# Version: PRODUCTION-STABLE-REV2.1-INLINE-STALE-RUNNING-CLEAR
# ------------------------------------------------------------
# 【概要】
#   schedule ライブラリの登録済み job を常駐実行する専用モジュール
#
# 【REV2.1】
#   - 旧 core/startup/ranking_entry_market_hours_skip_patch.py が
#     _is_job_running を差し替えていたstale-running-clearロジックを本文へ統合。
#
# 【REV1.0】
#   - schedule.run_pending() を常駐実行
#
# 【REV2.0 修正】
#   ✔ schedule.run_pending() の同期詰まりを回避
#   ✔ due job を検出して job ごとに非同期 thread dispatch
#   ✔ summary_parent_tick など重い job があっても loop が戻る
#   ✔ 同一 job の二重起動を防止
#   ✔ 前回 job 実行中の場合は skip して next_run を進める
#   ✔ heartbeat / jobs snapshot / running jobs snapshot を強化
#   ✔ global_data に loop / job 実行状態を書き込む
#   ✔ 例外で loop が死なないよう保護
#
# 【目的】
#   - schedule.every().minute.at(":00").do(...) で登録済みの job を
#     継続的に実行する
#   - 「run_summary_tick_once() では1回だけ表示されるが、
#      以後の定時サマリーが表示されない」問題を修正する
#   - 重い summary / ranking / yahoo job があっても run loop を止めない
#
# 【重要】
#   - scheduler_bootstrap.py は job 登録だけを担当
#   - schedule_loop.py は登録済み job の実行だけを担当
#   - startup.py / startup_orchestrator.py は起動順だけを担当
#
# 【期待ログ】
#   [startup.schedule_loop] thread started name=schedule-async-dispatch-loop alive=True
#   [startup.schedule_loop] started version=...
#   [startup.schedule_loop] dispatch start key=...
#   [startup.schedule_loop] job thread done key=... elapsed=...
#   [startup.schedule_loop] heartbeat loop=... jobs=... running=...
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import threading
import time
from typing import Any, Optional

import schedule

logger = logging.getLogger(__name__)

VERSION = "PRODUCTION-STABLE-REV2.0-SCHEDULE-ASYNC-DISPATCH-LOOP"


# ============================================================
# module state
# ============================================================

_SCHEDULE_LOOP_THREAD: Optional[threading.Thread] = None
_SCHEDULE_LOOP_STOP: Optional[threading.Event] = None
_SCHEDULE_LOOP_LOCK = threading.RLock()

_RUNNING_JOBS_LOCK = threading.RLock()
_RUNNING_JOBS: dict[str, dict[str, Any]] = {}

_JOB_STATS_LOCK = threading.RLock()
_JOB_STATS: dict[str, dict[str, Any]] = {}


# ============================================================
# global_data helpers
# ============================================================

def _get_global_data():
    try:
        from global_state import global_data  # type: ignore
        return global_data
    except Exception:
        pass

    try:
        from core.global_context.context import global_data  # type: ignore
        return global_data
    except Exception:
        return None


def _set_global_attr(name: str, value: Any) -> None:
    gd = _get_global_data()
    if gd is None:
        return

    try:
        setattr(gd, name, value)
    except Exception:
        pass


def _get_global_attr(name: str, default: Any = None) -> Any:
    gd = _get_global_data()
    if gd is None:
        return default

    try:
        return getattr(gd, name, default)
    except Exception:
        return default


# ============================================================
# job key / snapshot helpers
# ============================================================

def _safe_tags(job: Any) -> list[str]:
    try:
        return sorted([str(x) for x in (getattr(job, "tags", set()) or set())])
    except Exception:
        return []


def _job_func_name(job: Any) -> str:
    try:
        fn = getattr(job, "job_func", None)
        if fn is None:
            return "unknown"

        # functools.partial 対応
        real_fn = getattr(fn, "func", fn)
        name = getattr(real_fn, "__name__", None)
        module = getattr(real_fn, "__module__", None)

        if module and name:
            return f"{module}.{name}"
        if name:
            return str(name)

        return repr(fn)

    except Exception:
        return "unknown"


def _job_key(job: Any) -> str:
    """
    同一 job 判定用 key。

    tags がある場合は tags を優先。
    summary_parent_tick などは tag が安定しているため、
    repr(job) より tag の方が重複判定に向く。
    """
    tags = _safe_tags(job)
    if tags:
        return "tags:" + ",".join(tags)

    try:
        return "func:" + _job_func_name(job)
    except Exception:
        pass

    try:
        return "repr:" + repr(job)
    except Exception:
        return f"id:{id(job)}"


def _job_snapshot_one(job: Any) -> dict[str, Any]:
    try:
        return {
            "key": _job_key(job),
            "func": _job_func_name(job),
            "job": repr(job),
            "tags": _safe_tags(job),
            "next_run": str(getattr(job, "next_run", None)),
            "last_run": str(getattr(job, "last_run", None)),
            "interval": str(getattr(job, "interval", None)),
            "unit": str(getattr(job, "unit", None)),
            "should_run": bool(getattr(job, "should_run", False)),
        }
    except Exception:
        try:
            return {"job": repr(job)}
        except Exception:
            return {"job": "<unrepresentable>"}


def _safe_job_snapshot(limit: int = 50) -> list[dict[str, Any]]:
    snapshot: list[dict[str, Any]] = []

    try:
        jobs = list(getattr(schedule, "jobs", []) or [])
    except Exception:
        return snapshot

    for j in jobs[: int(limit)]:
        snapshot.append(_job_snapshot_one(j))

    return snapshot


def _running_jobs_snapshot() -> dict[str, Any]:
    with _RUNNING_JOBS_LOCK:
        out: dict[str, Any] = {}
        now = dt.datetime.now()

        for key, meta in list(_RUNNING_JOBS.items()):
            started_at = meta.get("started_at")
            elapsed = None
            try:
                if isinstance(started_at, dt.datetime):
                    elapsed = max(0.0, (now - started_at).total_seconds())
            except Exception:
                elapsed = None

            out[key] = {
                "thread": meta.get("thread_name"),
                "started_at": str(started_at),
                "elapsed_sec": elapsed,
                "job": meta.get("job_repr"),
                "tags": meta.get("tags"),
                "func": meta.get("func"),
            }

        return out


def _job_stats_snapshot(limit: int = 50) -> dict[str, Any]:
    with _JOB_STATS_LOCK:
        items = list(_JOB_STATS.items())[: int(limit)]
        return {k: dict(v) for k, v in items}


def get_schedule_loop_status() -> dict[str, Any]:
    """
    schedule loop の状態を返す。
    """
    with _SCHEDULE_LOOP_LOCK:
        alive = False
        name = None

        try:
            alive = bool(_SCHEDULE_LOOP_THREAD is not None and _SCHEDULE_LOOP_THREAD.is_alive())
            name = getattr(_SCHEDULE_LOOP_THREAD, "name", None)
        except Exception:
            alive = False
            name = None

        try:
            jobs = list(getattr(schedule, "jobs", []) or [])
            jobs_count = len(jobs)
        except Exception:
            jobs_count = 0

        return {
            "version": VERSION,
            "running": alive,
            "thread_name": name,
            "jobs_count": jobs_count,
            "loop_count": _get_global_attr("scheduler_run_pending_loop_count", 0),
            "last_at": _get_global_attr("scheduler_run_pending_loop_last_at", None),
            "started_at": _get_global_attr("scheduler_run_pending_loop_started_at", None),
            "running_jobs": _running_jobs_snapshot(),
            "job_stats": _job_stats_snapshot(limit=20),
            "snapshot": _safe_job_snapshot(limit=20),
        }


def log_schedule_jobs_snapshot(context: str = "snapshot", *, limit: int = 50) -> None:
    """
    schedule.jobs の snapshot をログ出力する。
    """
    try:
        jobs = list(getattr(schedule, "jobs", []) or [])
    except Exception:
        jobs = []

    logger.info(
        "[startup.schedule_loop] jobs snapshot context=%s jobs=%s running=%s snapshot=%s",
        context,
        len(jobs),
        _running_jobs_snapshot(),
        _safe_job_snapshot(limit=limit),
    )


# ============================================================
# job stats helpers
# ============================================================

def _stats_inc(key: str, field: str, amount: int = 1) -> None:
    with _JOB_STATS_LOCK:
        meta = _JOB_STATS.setdefault(key, {})
        meta[field] = int(meta.get(field, 0) or 0) + int(amount)


def _stats_set(key: str, **kwargs: Any) -> None:
    with _JOB_STATS_LOCK:
        meta = _JOB_STATS.setdefault(key, {})
        for k, v in kwargs.items():
            meta[k] = v


def _mark_job_running(key: str, job: Any, th: threading.Thread) -> None:
    with _RUNNING_JOBS_LOCK:
        _RUNNING_JOBS[key] = {
            "started_at": dt.datetime.now(),
            "thread_name": getattr(th, "name", None),
            "job_repr": repr(job),
            "tags": _safe_tags(job),
            "func": _job_func_name(job),
        }


def _mark_job_done(key: str) -> None:
    with _RUNNING_JOBS_LOCK:
        _RUNNING_JOBS.pop(key, None)


def _entry_stale_timeout_for_key(key: str) -> float:
    """旧 core/startup/ranking_entry_market_hours_skip_patch.py の_entry_stale_timeout_for_key。

    旧パッチの install() が os.environ.setdefault で確立していた実効デフォルト値
    (tonosama=60s, ranking=30s) をそのままこの関数の既定値として直接持たせる。
    """
    base = _env_float("ENTRY_SCHEDULER_STALE_RUNNING_CLEAR_SEC", 90.0)
    if "tonosama_entry" in key:
        return _env_float("TONOSAMA_ENTRY_SCHEDULER_STALE_SEC", 60.0)
    if "ranking_entry" in key:
        return _env_float("RANKING_ENTRY_SCHEDULER_STALE_SEC", 30.0)
    if "entry" in key:
        return base
    return 0.0


def _is_job_running(key: str) -> bool:
    """running中判定。旧 core/startup/ranking_entry_market_hours_skip_patch.py の
    _install_scheduler_stale_running_clear が差し替えていたstale-clearロジックを統合。

    entry/tonosama_entry/ranking_entry系のjobがタイムアウトを超えて running のまま
    残っている場合は、ここで自動的に _RUNNING_JOBS から取り除いて False (未実行中) を返す。
    """
    if not _env_bool("ENTRY_SCHEDULER_STALE_RUNNING_CLEAR_ENABLED", True):
        with _RUNNING_JOBS_LOCK:
            return key in _RUNNING_JOBS

    try:
        key_s = str(key)
        timeout_sec = _entry_stale_timeout_for_key(key_s)
        if timeout_sec > 0:
            with _RUNNING_JOBS_LOCK:
                meta = _RUNNING_JOBS.get(key_s)
                started_at = meta.get("started_at") if isinstance(meta, dict) else None
                elapsed = 0.0
                if isinstance(started_at, dt.datetime):
                    elapsed = max(0.0, (dt.datetime.now() - started_at).total_seconds())
                if meta and elapsed >= timeout_sec:
                    _RUNNING_JOBS.pop(key_s, None)
                    logger.warning(
                        "[SCHEDULE LOOP] cleared stale running key=%s elapsed=%.3fs timeout=%.3fs meta=%s",
                        key_s,
                        elapsed,
                        timeout_sec,
                        meta,
                    )
                    return False
    except Exception:
        logger.exception("[SCHEDULE LOOP] stale running check failed key=%s", key)

    with _RUNNING_JOBS_LOCK:
        return key in _RUNNING_JOBS


def _env_float(name: str, default: float) -> float:
    try:
        v = os.environ.get(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _env_bool(name: str, default: bool = False) -> bool:
    try:
        v = os.environ.get(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _is_ranking_summary_key(key: str) -> bool:
    s = str(key or "")
    return "ranking_summary_all" in s or "_run_ranking_summary_all_job_safe" in s


def _clear_if_stale(key: str) -> bool:
    """
    running とマークされた job が長時間残っている場合に強制解除する。

    ranking_summary_all が300秒以上 previous still running になり、
    次のランキングサマリー/AI判定を止め続けるケースを防ぐための保護。
    """
    stale_sec_default = _env_float("SCHEDULE_LOOP_STALE_JOB_SEC", 240.0)
    ranking_stale_sec = _env_float("RANKING_SUMMARY_STALE_JOB_SEC", 180.0)
    max_sec = ranking_stale_sec if _is_ranking_summary_key(key) else stale_sec_default

    try:
        with _RUNNING_JOBS_LOCK:
            meta = _RUNNING_JOBS.get(key)
            if not meta:
                return False
            started_at = meta.get("started_at")
            if not isinstance(started_at, dt.datetime):
                return False
            elapsed = max(0.0, (dt.datetime.now() - started_at).total_seconds())
            if elapsed < max_sec:
                return False
            _RUNNING_JOBS.pop(key, None)

        logger.warning(
            "[startup.schedule_loop] cleared stale running job key=%s elapsed=%.1fs max=%.1fs",
            key,
            elapsed,
            max_sec,
        )
        return True
    except Exception:
        logger.debug("[startup.schedule_loop] stale clear failed key=%s", key, exc_info=True)
        return False


def _safe_schedule_next_run(job: Any) -> None:
    """
    job が実行中で skip された場合などに next_run を進める。

    schedule.Job._schedule_next_run は private API だが、
    schedule.run_pending の同期詰まりを避けるための fallback として使う。
    """
    try:
        fn = getattr(job, "_schedule_next_run", None)
        if callable(fn):
            fn()
            return
    except Exception:
        logger.debug("[startup.schedule_loop] _schedule_next_run failed job=%s", repr(job), exc_info=True)

    # private API が使えない場合の最低限fallback
    try:
        interval = int(getattr(job, "interval", 1) or 1)
        unit = str(getattr(job, "unit", "") or "")

        delta = dt.timedelta(seconds=1)
        if unit in ("seconds", "second"):
            delta = dt.timedelta(seconds=interval)
        elif unit in ("minutes", "minute"):
            delta = dt.timedelta(minutes=interval)
        elif unit in ("hours", "hour"):
            delta = dt.timedelta(hours=interval)
        elif unit in ("days", "day"):
            delta = dt.timedelta(days=interval)

        setattr(job, "last_run", dt.datetime.now())
        setattr(job, "next_run", dt.datetime.now() + delta)

    except Exception:
        logger.debug("[startup.schedule_loop] manual next_run fallback failed job=%s", repr(job), exc_info=True)


# ============================================================
# due job dispatch
# ============================================================

def _get_due_jobs() -> list[Any]:
    """
    due になった job を返す。
    """
    try:
        jobs = list(getattr(schedule, "jobs", []) or [])
    except Exception:
        return []

    due: list[Any] = []

    for job in jobs:
        try:
            if bool(getattr(job, "should_run", False)):
                due.append(job)
        except Exception:
            logger.debug("[startup.schedule_loop] should_run check failed job=%s", repr(job), exc_info=True)

    return due


def _run_job_thread(job: Any, key: str) -> None:
    started = dt.datetime.now()
    ret = None

    try:
        _stats_inc(key, "run_count", 1)
        _stats_set(
            key,
            last_started_at=str(started),
            last_job_repr=repr(job),
            last_tags=_safe_tags(job),
            last_func=_job_func_name(job),
        )

        logger.info(
            "[startup.schedule_loop] job thread start key=%s tags=%s func=%s next_run=%s",
            key,
            _safe_tags(job),
            _job_func_name(job),
            getattr(job, "next_run", None),
        )

        # 重要:
        #   job.run() は job_func 実行後に next_run を更新する。
        #   ここは別thread内なので schedule loop 自体は詰まらない。
        ret = job.run()

        elapsed = max(0.0, (dt.datetime.now() - started).total_seconds())

        _stats_set(
            key,
            last_finished_at=str(dt.datetime.now()),
            last_elapsed_sec=elapsed,
            last_return=repr(ret),
            last_error=None,
        )

        logger.info(
            "[startup.schedule_loop] job thread done key=%s elapsed=%.3fs ret=%s next_run=%s",
            key,
            elapsed,
            ret,
            getattr(job, "next_run", None),
        )

    except Exception as e:
        elapsed = max(0.0, (dt.datetime.now() - started).total_seconds())

        _stats_inc(key, "error_count", 1)
        _stats_set(
            key,
            last_error_at=str(dt.datetime.now()),
            last_error=f"{type(e).__name__}: {e}",
            last_elapsed_sec=elapsed,
        )

        logger.exception(
            "[startup.schedule_loop] job thread failed key=%s elapsed=%.3fs job=%s",
            key,
            elapsed,
            repr(job),
        )

        # 例外で next_run が更新されなかった場合に無限即時再実行を避ける
        _safe_schedule_next_run(job)

    finally:
        _mark_job_done(key)

        try:
            _set_global_attr("scheduler_last_job_done_at", dt.datetime.now())
            _set_global_attr("scheduler_last_job_key", key)
        except Exception:
            pass


def _dispatch_due_job(job: Any, *, skip_if_running: bool = True) -> bool:
    key = _job_key(job)
    _clear_if_stale(key)

    if skip_if_running and _is_job_running(key):
        _stats_inc(key, "skip_running_count", 1)
        _stats_set(
            key,
            last_skip_at=str(dt.datetime.now()),
            last_skip_reason="previous_still_running",
        )

        logger.warning(
            "[startup.schedule_loop] job skipped because previous still running key=%s tags=%s func=%s next_run=%s running=%s",
            key,
            _safe_tags(job),
            _job_func_name(job),
            getattr(job, "next_run", None),
            _running_jobs_snapshot().get(key),
        )

        # 重要:
        #   next_run を進めないと、次のloopでも同じjobが即dueのままになり、
        #   skipログが連発する。
        _safe_schedule_next_run(job)
        return False

    th = threading.Thread(
        target=_run_job_thread,
        args=(job, key),
        name=f"schedule-job-{key[:80]}",
        daemon=True,
    )

    _mark_job_running(key, job, th)

    try:
        th.start()
    except Exception:
        _mark_job_done(key)
        _stats_inc(key, "dispatch_error_count", 1)
        logger.exception("[startup.schedule_loop] job dispatch failed key=%s job=%s", key, repr(job))
        _safe_schedule_next_run(job)
        return False

    _stats_inc(key, "dispatch_count", 1)
    _stats_set(key, last_dispatched_at=str(dt.datetime.now()))

    logger.info(
        "[startup.schedule_loop] dispatch start key=%s thread=%s tags=%s func=%s next_run=%s",
        key,
        th.name,
        _safe_tags(job),
        _job_func_name(job),
        getattr(job, "next_run", None),
    )

    return True


def dispatch_due_jobs_once(*, skip_if_running: bool = True) -> int:
    """
    due jobs を1回だけ非同期dispatchする。
    """
    due_jobs = _get_due_jobs()
    dispatched = 0

    if not due_jobs:
        return 0

    logger.info(
        "[startup.schedule_loop] due jobs detected count=%s keys=%s",
        len(due_jobs),
        [_job_key(j) for j in due_jobs],
    )

    for job in due_jobs:
        try:
            if _dispatch_due_job(job, skip_if_running=skip_if_running):
                dispatched += 1
        except Exception:
            logger.exception(
                "[startup.schedule_loop] dispatch_due_jobs_once failed job=%s",
                repr(job),
            )

    return dispatched


# ============================================================
# loop controls
# ============================================================

def is_schedule_run_pending_loop_running() -> bool:
    with _SCHEDULE_LOOP_LOCK:
        try:
            return bool(_SCHEDULE_LOOP_THREAD is not None and _SCHEDULE_LOOP_THREAD.is_alive())
        except Exception:
            return False


def stop_schedule_run_pending_loop_safe(timeout: float = 3.0) -> bool:
    """
    schedule async dispatch loop を停止する。
    通常運用では呼ばなくてよい。
    """
    global _SCHEDULE_LOOP_THREAD, _SCHEDULE_LOOP_STOP

    with _SCHEDULE_LOOP_LOCK:
        stop_event = _SCHEDULE_LOOP_STOP
        th = _SCHEDULE_LOOP_THREAD

        if stop_event is None or th is None:
            _set_global_attr("scheduler_run_pending_loop_running", False)
            return True

        try:
            stop_event.set()
        except Exception:
            pass

    try:
        if th is not None and th.is_alive():
            th.join(timeout=float(timeout))
    except Exception:
        logger.debug("[startup.schedule_loop] join failed", exc_info=True)

    alive = False
    try:
        alive = bool(th is not None and th.is_alive())
    except Exception:
        alive = False

    if alive:
        logger.warning("[startup.schedule_loop] stop requested but thread still alive")
        return False

    with _SCHEDULE_LOOP_LOCK:
        _SCHEDULE_LOOP_THREAD = None
        _SCHEDULE_LOOP_STOP = None

    _set_global_attr("scheduler_run_pending_loop_running", False)
    logger.info("[startup.schedule_loop] stopped safely")
    return True


def start_schedule_run_pending_loop_safe(
    *,
    interval_seconds: float = 0.5,
    heartbeat_seconds: float = 30.0,
    snapshot_limit: int = 30,
    skip_if_running: bool = True,
) -> bool:
    """
    schedule 登録済み job の非同期 dispatch loop を起動する。

    重要:
      旧REV1.0では schedule.run_pending() を直接呼んでいた。
      しかし schedule.run_pending() は同期実行のため、
      summary_parent_tick が重いと loop が戻らず、次の定時tickを逃す。

      REV2.0では due job を検出し、job.run() を別threadへdispatchする。
      これにより loop は継続し、他の due job を検出し続けられる。

    Args:
        interval_seconds:
            due job チェック間隔。
        heartbeat_seconds:
            heartbeat snapshot を出す間隔。
        snapshot_limit:
            heartbeat に出す jobs snapshot の最大数。
        skip_if_running:
            同一 job がまだ実行中なら次回分を skip する。
    """
    global _SCHEDULE_LOOP_THREAD, _SCHEDULE_LOOP_STOP

    with _SCHEDULE_LOOP_LOCK:
        try:
            if _SCHEDULE_LOOP_THREAD is not None and _SCHEDULE_LOOP_THREAD.is_alive():
                logger.info(
                    "[startup.schedule_loop] already running thread=%s status=%s",
                    _SCHEDULE_LOOP_THREAD.name,
                    get_schedule_loop_status(),
                )
                _set_global_attr("scheduler_run_pending_loop_running", True)
                return True
        except Exception:
            logger.debug("[startup.schedule_loop] running check failed", exc_info=True)

        stop_event = threading.Event()
        _SCHEDULE_LOOP_STOP = stop_event

        interval_seconds = max(0.1, float(interval_seconds))
        heartbeat_seconds = max(5.0, float(heartbeat_seconds))
        snapshot_limit = max(1, int(snapshot_limit))

        def _loop() -> None:
            loop_count = 0
            last_snapshot_at = 0.0

            _set_global_attr("scheduler_run_pending_loop_running", True)
            _set_global_attr("scheduler_run_pending_loop_started_at", dt.datetime.now())
            _set_global_attr("scheduler_run_pending_loop_version", VERSION)

            logger.info(
                "[startup.schedule_loop] started version=%s interval_seconds=%.3f heartbeat_seconds=%.1f skip_if_running=%s",
                VERSION,
                interval_seconds,
                heartbeat_seconds,
                skip_if_running,
            )

            log_schedule_jobs_snapshot("loop-start", limit=snapshot_limit)

            while not stop_event.is_set():
                loop_count += 1

                try:
                    jobs = list(getattr(schedule, "jobs", []) or [])
                    now_ts = time.time()

                    dispatched = dispatch_due_jobs_once(skip_if_running=skip_if_running)

                    if now_ts - last_snapshot_at >= heartbeat_seconds:
                        last_snapshot_at = now_ts

                        logger.info(
                            "[startup.schedule_loop] heartbeat loop=%s jobs=%s dispatched=%s running=%s stats=%s snapshot=%s",
                            loop_count,
                            len(jobs),
                            dispatched,
                            _running_jobs_snapshot(),
                            _job_stats_snapshot(limit=snapshot_limit),
                            _safe_job_snapshot(limit=snapshot_limit),
                        )

                    _set_global_attr("scheduler_run_pending_loop_running", True)
                    _set_global_attr("scheduler_run_pending_loop_last_at", dt.datetime.now())
                    _set_global_attr("scheduler_run_pending_loop_count", loop_count)
                    _set_global_attr("scheduler_jobs_count", len(jobs))
                    _set_global_attr("scheduler_running_jobs", _running_jobs_snapshot())
                    _set_global_attr("scheduler_job_stats", _job_stats_snapshot(limit=50))

                except Exception:
                    logger.exception("[startup.schedule_loop] loop iteration failed")

                    try:
                        current_errors = int(_get_global_attr("scheduler_run_pending_loop_errors", 0) or 0)
                        _set_global_attr("scheduler_run_pending_loop_errors", current_errors + 1)
                        _set_global_attr("scheduler_run_pending_loop_last_error_at", dt.datetime.now())
                    except Exception:
                        pass

                try:
                    time.sleep(interval_seconds)
                except Exception:
                    pass

            _set_global_attr("scheduler_run_pending_loop_running", False)
            _set_global_attr("scheduler_run_pending_loop_stopped_at", dt.datetime.now())

            logger.info("[startup.schedule_loop] stopped loop_count=%s", loop_count)

        th = threading.Thread(
            target=_loop,
            name="schedule-async-dispatch-loop",
            daemon=True,
        )

        th.start()

        _SCHEDULE_LOOP_THREAD = th

    alive = False
    try:
        alive = bool(th.is_alive())
    except Exception:
        alive = False

    _set_global_attr("scheduler_run_pending_loop_running", alive)

    logger.info(
        "[startup.schedule_loop] thread started name=%s alive=%s status=%s",
        getattr(th, "name", None),
        alive,
        get_schedule_loop_status(),
    )

    return alive


# ============================================================
# compatibility alias
# ============================================================

def _start_schedule_run_pending_loop_safe(*args: Any, **kwargs: Any) -> bool:
    """
    旧 startup.py 内関数名との互換 alias。
    """
    return start_schedule_run_pending_loop_safe(*args, **kwargs)


__all__ = [
    "VERSION",
    "start_schedule_run_pending_loop_safe",
    "_start_schedule_run_pending_loop_safe",
    "stop_schedule_run_pending_loop_safe",
    "is_schedule_run_pending_loop_running",
    "get_schedule_loop_status",
    "log_schedule_jobs_snapshot",
    "dispatch_due_jobs_once",
]