# ============================================================
# File   : scheduler_jobs/summary/scheduler.py
# Function:
#   - 定時サマリーの scheduler 登録を担当する
#   - PUSH由来サマリー / RANKING由来サマリーの親tickを登録する
#   - 毎時0分起点で 1分 / 3分 / 5分 の time-locked 実行を制御する
#   - 既定では PUSH のみ動かし、RANKING は明示有効時のみ動かす
#   - 時間外でも 1m / 3m / 5m 周期で最新確定サマリー表示を継続させる
#   - 統合親tick方式と旧互換APIの両方を維持する
# ------------------------------------------------------------
# Version: Ver32.4-PRODUCTION-SUMMARY-SCHEDULER-PUSH-FALLBACK-ASYNC-STALE-RESET
#          -TIMEOUT-GUARD
#          -PUSH-DEFAULT
#          -RANKING-OPTIONAL
#          -DISPLAY-TRACE-ENHANCED
#          -REGISTRATION-TAG-GUARD
#          -TIME-LOCKED-BASE-00
#          -CLOSED-MARKET-PERIODIC-DISPLAY
#          -SUMMARY-ENTRY-PIPELINE-SAFE
#          -PARENT-TICK-DIAG
#          -INDIVIDUAL-FALLBACK-ON-UNIFIED-FAIL
#          -PARENT-TICK-TIMEOUT-GUARD
#          -PREVIOUS-STILL-RUNNING-PUSH-FALLBACK
#          -ASYNC-PUSH-FALLBACK
#          -STALE-FALLBACK-RESET
# ------------------------------------------------------------
# ✔ PUSH由来サマリーの定時登録
# ✔ ランキング由来サマリーの定時登録（明示有効時のみ）
# ✔ :00 基準で 1m / 3m / 5m 実行
# ✔ calendar_utils.should_run_interval を利用
# ✔ 1m / 3m / 5m を独立 try/except で保護
# ✔ PUSH / RANKING を1つの親tickで統一制御
# ✔ 旧 scheduler 互換名も維持
# ✔ 定時表示されない問題の切り分け用ログを追加
# ✔ 二重登録回避のため tag clear を追加
# ✔ 既定では PUSH のみ実行
# ✔ RANKING はフラグ有効時のみ親tickで実行
# ✔ 時間外でも 1m/3m/5m 周期で最新確定サマリーを表示
# ✔ run_entry フラグを runners へ伝搬
# ✔ unified runner 失敗時に PUSH 個別tickへ fallback
# ✔ unified runner が詰まっても親tickを必ず返す
# ✔ previous_unified_bg_still_running 時も PUSH summary だけは fallback 実行
# ✔ unified timeout 時も PUSH summary だけは fallback 実行
# ✔ Yahoo補完やRanking summaryが重くても最新20分のPUSH summary保存を止めない
#
# Ver32.4:
# ✔ PUSH fallback を親tick内で同期実行しない
# ✔ PUSH fallback を専用 daemon thread で起動
# ✔ previous_push_fallback_still_running が永久に残らないよう stale reset
# ✔ fallback実行中でも一定秒数超過なら新規fallbackを許可
# ✔ 親tickを短時間で返して schedule_loop の詰まりを軽減
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import time
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from typing import Any, Callable, Optional

import schedule

from scheduler_jobs.summary.calendar_utils import should_run_interval
from scheduler_jobs.summary.runners import (
    job_1m as job_push_summary_1m,
    job_3m as job_push_summary_3m,
    job_5m as job_push_summary_5m,
    job_ranking_1m,
    job_ranking_3m,
    job_ranking_5m,
    run_time_locked_summary_jobs,
)

logger = logging.getLogger(__name__)

TAG_PUSH = "summary_push_tick"
TAG_RANKING = "summary_ranking_tick"
TAG_PARENT = "summary_parent_tick"


# ============================================================
# feature flags
# ============================================================

def _env_flag(name: str, default: bool = False) -> bool:
    try:
        raw = str(os.getenv(name, "")).strip().lower()
        if raw in ("1", "true", "yes", "on", "enable", "enabled"):
            return True
        if raw in ("0", "false", "no", "off", "disable", "disabled"):
            return False
    except Exception:
        pass
    return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        raw = str(os.getenv(name, "")).strip()
        if raw:
            v = float(raw)
            if v > 0:
                return v
    except Exception:
        pass
    return float(default)


def _ranking_enabled() -> bool:
    return _env_flag("ENABLE_RANKING_SUMMARY_TICK", default=False)


def _summary_entry_enabled() -> bool:
    return _env_flag("ENABLE_SUMMARY_ENTRY_TICK", default=True)


def _summary_debug_enabled() -> bool:
    return _env_flag("ENABLE_SUMMARY_TICK_DEBUG", default=True)


def _fallback_individual_enabled() -> bool:
    return _env_flag("ENABLE_SUMMARY_UNIFIED_FALLBACK", default=True)


def _timeout_guard_enabled() -> bool:
    return _env_flag("ENABLE_SUMMARY_TICK_TIMEOUT_GUARD", default=True)


def _push_fallback_when_blocked_enabled() -> bool:
    """
    unified parent tick が詰まっている場合でも、
    PUSH summary だけは個別に実行するか。

    最新20分はYahooではなくPUSHで埋める必要があるため、
    デフォルト True。
    """
    return _env_flag("ENABLE_PUSH_SUMMARY_FALLBACK_WHEN_UNIFIED_BLOCKED", default=True)


def _parent_timeout_sec() -> float:
    # PUSH 1m summary が 35秒 timeout で CALL timeout になり、entry 実行前に
    # scheduler 側で失敗扱いになる問題を緩和するため 45 -> 120 に緩和
    # (旧 core/startup/summary_scheduler_timeout_patch.py から移設)。
    return _env_float("SUMMARY_PARENT_TICK_TIMEOUT_SEC", default=120.0)


def _child_timeout_sec() -> float:
    return _env_float("SUMMARY_CHILD_JOB_TIMEOUT_SEC", default=90.0)


def _push_fallback_stale_sec() -> float:
    """
    PUSH fallback が実行中のまま残ったと判断する秒数。

    fallback 内部では 1m/3m/5m の子job timeout があるため、
    通常は 35秒 x 3 = 105秒程度以内に戻る想定。
    余裕を見てデフォルト300秒
    (旧 core/startup/summary_scheduler_timeout_patch.py から移設)。
    """
    return _env_float("SUMMARY_PUSH_FALLBACK_STALE_SEC", default=300.0)


def _push_fallback_async_enabled() -> bool:
    """
    PUSH fallback を親tickから別スレッド起動するか。
    デフォルト True。
    """
    return _env_flag("ENABLE_SUMMARY_PUSH_FALLBACK_ASYNC", default=True)


# ============================================================
# timeout executor
# ============================================================

_timeout_executor = ThreadPoolExecutor(
    max_workers=4,
    thread_name_prefix="summary-timeout-guard",
)

_unified_bg_lock = threading.Lock()
_unified_bg_running = False
_unified_bg_started_at: Optional[float] = None

# Ver32.4:
# Lockだけだと previous_push_fallback_still_running が残った場合に解除不能になるため、
# 状態管理型に変更する。
_push_fallback_state_lock = threading.RLock()
_push_fallback_running = False
_push_fallback_started_at: Optional[float] = None
_push_fallback_reason: Optional[str] = None
_push_fallback_thread_name: Optional[str] = None


def _run_with_timeout(
    fn: Callable[..., Any],
    *,
    timeout_sec: float,
    label: str,
    kwargs: Optional[dict[str, Any]] = None,
) -> tuple[bool, bool, Any]:
    """
    Returns:
        (ok, timed_out, result)

    注意:
      Python thread は強制停止できない。
      ただし親tickは timeout で必ず戻す。
      timeoutした実処理は executor 側で継続する可能性がある。
    """
    kwargs = kwargs or {}

    if not _timeout_guard_enabled():
        try:
            return True, False, fn(**kwargs)
        except Exception:
            logger.exception("[summary.scheduler] timeout wrapper direct failed label=%s", label)
            return False, False, None

    future = _timeout_executor.submit(fn, **kwargs)
    try:
        result = future.result(timeout=timeout_sec)
        return True, False, result
    except FuturesTimeoutError:
        logger.error(
            "[summary.scheduler] TIMEOUT label=%s timeout=%.1fs fn=%s",
            label,
            timeout_sec,
            _safe_job_name(fn),
        )
        return False, True, None
    except Exception:
        logger.exception(
            "[summary.scheduler] exception in timeout wrapper label=%s fn=%s",
            label,
            _safe_job_name(fn),
        )
        return False, False, None


# ============================================================
# basic helpers
# ============================================================

def _now(now: Optional[dt.datetime] = None) -> dt.datetime:
    return (now or dt.datetime.now()).replace(second=0, microsecond=0)


def _should_run_1m(now: Optional[dt.datetime] = None) -> bool:
    _ = _now(now)
    return True


def _should_run_3m(now: Optional[dt.datetime] = None) -> bool:
    n = _now(now)
    try:
        return bool(should_run_interval(3, now=n))
    except Exception:
        return n.minute % 3 == 0


def _should_run_5m(now: Optional[dt.datetime] = None) -> bool:
    n = _now(now)
    try:
        return bool(should_run_interval(5, now=n))
    except Exception:
        return n.minute % 5 == 0


def should_run_1m(now: Optional[dt.datetime] = None) -> bool:
    return _should_run_1m(now)


def should_run_3m(now: Optional[dt.datetime] = None) -> bool:
    return _should_run_3m(now)


def should_run_5m(now: Optional[dt.datetime] = None) -> bool:
    return _should_run_5m(now)


def _run_flags(now: Optional[dt.datetime] = None) -> tuple[bool, bool, bool]:
    n = _now(now)
    return _should_run_1m(n), _should_run_3m(n), _should_run_5m(n)


def _safe_job_name(fn) -> str:
    try:
        return getattr(fn, "__name__", repr(fn))
    except Exception:
        return "unknown"


def _clear_tag(tag: str) -> None:
    try:
        schedule.clear(tag)
        logger.info("[summary.scheduler] cleared existing scheduled jobs tag=%s", tag)
    except Exception:
        logger.exception("[summary.scheduler] failed to clear tag=%s", tag)


def _log_schedule_snapshot(context: str) -> None:
    try:
        jobs = list(getattr(schedule, "jobs", []) or [])
        payload = []
        for j in jobs:
            try:
                payload.append({
                    "job": repr(j),
                    "tags": list(getattr(j, "tags", set()) or set()),
                    "next_run": str(getattr(j, "next_run", None)),
                    "last_run": str(getattr(j, "last_run", None)),
                })
            except Exception:
                payload.append({"job": repr(j), "tags": []})

        logger.info(
            "[summary.scheduler] %s scheduled_jobs=%s detail=%s",
            context,
            len(jobs),
            payload,
        )
    except Exception:
        logger.exception("[summary.scheduler] %s schedule snapshot failed", context)


def _debug_tick_boundary(now: dt.datetime, context: str) -> None:
    if not _summary_debug_enabled():
        return
    try:
        run_1m, run_3m, run_5m = _run_flags(now)
        logger.info(
            "[SUMMARY TICK DEBUG] context=%s now=%s minute=%s "
            "run_1m=%s run_3m=%s run_5m=%s "
            "minute_mod_3=%s minute_mod_5=%s ranking_enabled=%s run_entry=%s "
            "timeout_guard=%s parent_timeout=%.1f child_timeout=%.1f "
            "push_fallback_when_blocked=%s fallback_async=%s fallback_stale=%.1f",
            context,
            now,
            now.minute,
            run_1m,
            run_3m,
            run_5m,
            now.minute % 3,
            now.minute % 5,
            _ranking_enabled(),
            _summary_entry_enabled(),
            _timeout_guard_enabled(),
            _parent_timeout_sec(),
            _child_timeout_sec(),
            _push_fallback_when_blocked_enabled(),
            _push_fallback_async_enabled(),
            _push_fallback_stale_sec(),
        )
    except Exception:
        logger.exception("[SUMMARY TICK DEBUG] boundary log failed context=%s", context)


def _call_job_compat(
    fn,
    *,
    label: str,
    interval: int,
    now: dt.datetime,
    run_entry: Optional[bool] = None,
):
    if run_entry is None:
        try:
            return fn(display=True, now=now)
        except TypeError:
            return fn(display=True)

    try:
        return fn(display=True, now=now, run_entry=run_entry)
    except TypeError:
        try:
            return fn(display=True, run_entry=run_entry)
        except TypeError:
            return fn(display=True)


def _invoke_job(
    fn,
    *,
    label: str,
    interval: int,
    now: dt.datetime,
    run_entry: Optional[bool] = None,
) -> None:
    fn_name = _safe_job_name(fn)
    t0 = time.perf_counter()

    logger.info(
        "[summary.scheduler] CALL start label=%s fn=%s interval=%s hhmm=%02d:%02d "
        "display=True run_entry=%s timeout=%.1fs",
        label,
        fn_name,
        interval,
        now.hour,
        now.minute,
        run_entry,
        _child_timeout_sec(),
    )

    ok, timed_out, result = _run_with_timeout(
        _call_job_compat,
        timeout_sec=_child_timeout_sec(),
        label=f"{label}-{interval}m",
        kwargs={
            "fn": fn,
            "label": label,
            "interval": interval,
            "now": now,
            "run_entry": run_entry,
        },
    )

    elapsed = time.perf_counter() - t0

    if timed_out:
        logger.error(
            "[summary.scheduler] CALL timeout label=%s fn=%s interval=%s elapsed=%.3fs",
            label,
            fn_name,
            interval,
            elapsed,
        )
        return

    if not ok:
        logger.error(
            "[summary.scheduler] CALL failed label=%s fn=%s interval=%s elapsed=%.3fs",
            label,
            fn_name,
            interval,
            elapsed,
        )
        return

    logger.info(
        "[summary.scheduler] CALL success label=%s fn=%s interval=%s result_type=%s elapsed=%.3fs",
        label,
        fn_name,
        interval,
        type(result).__name__,
        elapsed,
    )


# ============================================================
# source runners
# ============================================================

def _run_push_summary_tick(now: Optional[dt.datetime] = None) -> None:
    try:
        now = _now(now)
        minute = now.minute
        run_1m, run_3m, run_5m = _run_flags(now)
        run_entry = _summary_entry_enabled()

        _debug_tick_boundary(now, "push_tick")

        logger.info(
            "[summary.scheduler] PUSH tick start hhmm=%02d:%02d "
            "run_1m=%s run_3m=%s run_5m=%s run_entry=%s",
            now.hour,
            minute,
            run_1m,
            run_3m,
            run_5m,
            run_entry,
        )

        if run_1m:
            _invoke_job(job_push_summary_1m, label="PUSH", interval=1, now=now, run_entry=run_entry)
        else:
            logger.info("[summary.scheduler] PUSH 1m skipped hhmm=%02d:%02d", now.hour, minute)

        if run_3m:
            _invoke_job(job_push_summary_3m, label="PUSH", interval=3, now=now, run_entry=run_entry)
        else:
            logger.info(
                "[summary.scheduler] PUSH 3m skipped hhmm=%02d:%02d reason=not_interval_boundary",
                now.hour,
                minute,
            )

        if run_5m:
            _invoke_job(job_push_summary_5m, label="PUSH", interval=5, now=now, run_entry=run_entry)
        else:
            logger.info(
                "[summary.scheduler] PUSH 5m skipped hhmm=%02d:%02d reason=not_interval_boundary",
                now.hour,
                minute,
            )

        logger.info("[summary.scheduler] PUSH tick finished hhmm=%02d:%02d", now.hour, minute)

    except Exception:
        logger.exception("[summary.scheduler] _run_push_summary_tick failed")


# ============================================================
# PUSH fallback state / async worker
# ============================================================

def _push_fallback_state_snapshot() -> dict[str, Any]:
    with _push_fallback_state_lock:
        elapsed = None
        if _push_fallback_started_at is not None:
            try:
                elapsed = max(0.0, time.time() - float(_push_fallback_started_at))
            except Exception:
                elapsed = None

        return {
            "running": _push_fallback_running,
            "started_at_ts": _push_fallback_started_at,
            "elapsed_sec": elapsed,
            "reason": _push_fallback_reason,
            "thread": _push_fallback_thread_name,
        }


def _try_mark_push_fallback_running(*, reason: str) -> tuple[bool, str]:
    """
    PUSH fallback の二重起動を防ぐ。
    ただし stale 秒数を超えた場合は、古いrunning状態を捨てて再起動を許可する。
    """
    global _push_fallback_running, _push_fallback_started_at
    global _push_fallback_reason, _push_fallback_thread_name

    now_ts = time.time()
    stale_sec = _push_fallback_stale_sec()

    with _push_fallback_state_lock:
        if _push_fallback_running:
            elapsed = max(0.0, now_ts - float(_push_fallback_started_at or now_ts))

            if elapsed < stale_sec:
                return False, f"previous_push_fallback_still_running elapsed={elapsed:.1f}s stale={stale_sec:.1f}s"

            logger.warning(
                "[summary.scheduler] PUSH fallback stale running reset "
                "old_reason=%s old_thread=%s elapsed=%.3fs stale=%.3fs new_reason=%s",
                _push_fallback_reason,
                _push_fallback_thread_name,
                elapsed,
                stale_sec,
                reason,
            )

        _push_fallback_running = True
        _push_fallback_started_at = now_ts
        _push_fallback_reason = reason
        _push_fallback_thread_name = threading.current_thread().name

        return True, "marked_running"


def _mark_push_fallback_done() -> None:
    global _push_fallback_running, _push_fallback_started_at
    global _push_fallback_reason, _push_fallback_thread_name

    with _push_fallback_state_lock:
        _push_fallback_running = False
        _push_fallback_started_at = None
        _push_fallback_reason = None
        _push_fallback_thread_name = None


def _push_fallback_worker(now: dt.datetime, *, reason: str) -> None:
    t0 = time.perf_counter()
    thread_name = threading.current_thread().name

    try:
        with _push_fallback_state_lock:
            global _push_fallback_thread_name
            _push_fallback_thread_name = thread_name

        run_1m, run_3m, run_5m = _run_flags(now)

        logger.warning(
            "[summary.scheduler] PUSH fallback worker start reason=%s hhmm=%02d:%02d "
            "run_1m=%s run_3m=%s run_5m=%s thread=%s",
            reason,
            now.hour,
            now.minute,
            run_1m,
            run_3m,
            run_5m,
            thread_name,
        )

        _run_push_summary_tick(now=now)

        logger.warning(
            "[summary.scheduler] PUSH fallback worker done reason=%s hhmm=%02d:%02d "
            "elapsed=%.3fs thread=%s",
            reason,
            now.hour,
            now.minute,
            time.perf_counter() - t0,
            thread_name,
        )

    except Exception:
        logger.exception(
            "[summary.scheduler] PUSH fallback worker failed reason=%s hhmm=%02d:%02d "
            "elapsed=%.3fs thread=%s",
            reason,
            now.hour,
            now.minute,
            time.perf_counter() - t0,
            thread_name,
        )
    finally:
        _mark_push_fallback_done()


def _run_push_fallback_when_unified_blocked(now: dt.datetime, *, reason: str) -> None:
    """
    unified parent tick / ranking / yahoo 側の重い処理が詰まっても、
    最新20分を埋めるために PUSH summary だけは独立実行する。

    Ver32.4:
      - 親tick内では同期実行しない
      - 専用daemon threadでPUSH fallbackを走らせる
      - previous_push_fallback_still_running が古い場合は stale reset する
    """
    if not _push_fallback_when_blocked_enabled():
        logger.warning(
            "[summary.scheduler] PUSH fallback disabled reason=%s hhmm=%02d:%02d",
            reason,
            now.hour,
            now.minute,
        )
        return

    marked, detail = _try_mark_push_fallback_running(reason=reason)

    if not marked:
        logger.warning(
            "[summary.scheduler] PUSH fallback skipped reason=%s detail=%s "
            "hhmm=%02d:%02d state=%s",
            reason,
            detail,
            now.hour,
            now.minute,
            _push_fallback_state_snapshot(),
        )
        return

    if not _push_fallback_async_enabled():
        logger.warning(
            "[summary.scheduler] PUSH fallback sync start reason=%s hhmm=%02d:%02d",
            reason,
            now.hour,
            now.minute,
        )
        _push_fallback_worker(now, reason=reason)
        return

    try:
        th = threading.Thread(
            target=_push_fallback_worker,
            kwargs={"now": now, "reason": reason},
            name=f"summary-push-fallback-{reason[:40]}-{now.strftime('%H%M')}",
            daemon=True,
        )

        with _push_fallback_state_lock:
            global _push_fallback_thread_name
            _push_fallback_thread_name = th.name

        th.start()

        logger.warning(
            "[summary.scheduler] PUSH fallback dispatched async reason=%s "
            "hhmm=%02d:%02d thread=%s state=%s",
            reason,
            now.hour,
            now.minute,
            th.name,
            _push_fallback_state_snapshot(),
        )

    except Exception:
        _mark_push_fallback_done()
        logger.exception(
            "[summary.scheduler] PUSH fallback async dispatch failed reason=%s hhmm=%02d:%02d",
            reason,
            now.hour,
            now.minute,
        )


def _run_ranking_summary_tick(now: Optional[dt.datetime] = None) -> None:
    try:
        now = _now(now)
        minute = now.minute
        run_1m, run_3m, run_5m = _run_flags(now)

        _debug_tick_boundary(now, "ranking_tick")

        logger.info(
            "[summary.scheduler] RANKING tick start hhmm=%02d:%02d "
            "run_1m=%s run_3m=%s run_5m=%s",
            now.hour,
            minute,
            run_1m,
            run_3m,
            run_5m,
        )

        if run_1m:
            _invoke_job(job_ranking_1m, label="RANKING", interval=1, now=now, run_entry=None)
        else:
            logger.info("[summary.scheduler] RANKING 1m skipped hhmm=%02d:%02d", now.hour, minute)

        if run_3m:
            _invoke_job(job_ranking_3m, label="RANKING", interval=3, now=now, run_entry=None)
        else:
            logger.info(
                "[summary.scheduler] RANKING 3m skipped hhmm=%02d:%02d reason=not_interval_boundary",
                now.hour,
                minute,
            )

        if run_5m:
            _invoke_job(job_ranking_5m, label="RANKING", interval=5, now=now, run_entry=None)
        else:
            logger.info(
                "[summary.scheduler] RANKING 5m skipped hhmm=%02d:%02d reason=not_interval_boundary",
                now.hour,
                minute,
            )

        logger.info("[summary.scheduler] RANKING tick finished hhmm=%02d:%02d", now.hour, minute)

    except Exception:
        logger.exception("[summary.scheduler] _run_ranking_summary_tick failed")


# ============================================================
# unified parent tick
# ============================================================

def _call_unified_runner(now: dt.datetime, ranking_enabled: bool, run_entry: bool):
    return run_time_locked_summary_jobs(
        now=now,
        run_push=True,
        run_ranking=ranking_enabled,
        display=True,
        run_entry=run_entry,
    )


def _run_summary_tick(now: Optional[dt.datetime] = None) -> None:
    """
    毎分 :00 に1回だけ呼ばれる統合親tick。

    Ver32.4:
      - unified runner が previous_still_running の場合でも、
        PUSH fallback をasync起動して最新20分のPUSH summary保存を止めない。
      - unified runner timeout 時も、PUSH fallback をasync起動する。
      - fallback running が古い場合は stale reset して再投入できる。
    """
    global _unified_bg_running, _unified_bg_started_at

    now = _now(now)
    run_1m, run_3m, run_5m = _run_flags(now)
    ranking_enabled = _ranking_enabled()
    run_entry = _summary_entry_enabled()
    t0 = time.perf_counter()

    _debug_tick_boundary(now, "parent_tick")

    logger.info(
        "[summary.scheduler] parent tick start hhmm=%02d:%02d "
        "run_1m=%s run_3m=%s run_5m=%s ranking_enabled=%s run_entry=%s "
        "timeout=%.1fs fallback_state=%s",
        now.hour,
        now.minute,
        run_1m,
        run_3m,
        run_5m,
        ranking_enabled,
        run_entry,
        _parent_timeout_sec(),
        _push_fallback_state_snapshot(),
    )

    with _unified_bg_lock:
        if _unified_bg_running:
            elapsed_unified = None
            try:
                if _unified_bg_started_at is not None:
                    elapsed_unified = max(0.0, time.time() - float(_unified_bg_started_at))
            except Exception:
                elapsed_unified = None

            logger.warning(
                "[summary.scheduler] parent tick skipped unified reason=previous_unified_bg_still_running "
                "hhmm=%02d:%02d unified_elapsed=%s parent_elapsed=%.3fs -> dispatch PUSH fallback",
                now.hour,
                now.minute,
                f"{elapsed_unified:.1f}s" if elapsed_unified is not None else "-",
                time.perf_counter() - t0,
            )

            _run_push_fallback_when_unified_blocked(
                now,
                reason="previous_unified_bg_still_running",
            )
            return

        _unified_bg_running = True
        _unified_bg_started_at = time.time()

    unified_ok = False
    unified_timeout = False

    try:
        def _wrapped_unified():
            global _unified_bg_running, _unified_bg_started_at
            try:
                return _call_unified_runner(now, ranking_enabled, run_entry)
            finally:
                with _unified_bg_lock:
                    _unified_bg_running = False
                    _unified_bg_started_at = None

        ok, timed_out, result = _run_with_timeout(
            _wrapped_unified,
            timeout_sec=_parent_timeout_sec(),
            label="unified-parent",
        )

        unified_ok = bool(ok and not timed_out)
        unified_timeout = bool(timed_out)

        if unified_timeout:
            logger.error(
                "[summary.scheduler] parent tick unified runner TIMEOUT -> dispatch PUSH fallback "
                "hhmm=%02d:%02d elapsed=%.3fs",
                now.hour,
                now.minute,
                time.perf_counter() - t0,
            )

            _run_push_fallback_when_unified_blocked(
                now,
                reason="unified_parent_timeout",
            )
            return

        if unified_ok:
            logger.info(
                "[summary.scheduler] parent tick -> unified runner success result_type=%s elapsed=%.3fs",
                type(result).__name__,
                time.perf_counter() - t0,
            )
        else:
            logger.error(
                "[summary.scheduler] parent tick -> unified runner failed elapsed=%.3fs",
                time.perf_counter() - t0,
            )

    except Exception:
        with _unified_bg_lock:
            _unified_bg_running = False
            _unified_bg_started_at = None
        logger.exception("[summary.scheduler] parent tick -> unified runner wrapper failed")

    if not unified_ok and not unified_timeout and _fallback_individual_enabled():
        logger.warning(
            "[summary.scheduler] parent tick fallback start -> individual PUSH tick hhmm=%02d:%02d",
            now.hour,
            now.minute,
        )
        try:
            _run_push_summary_tick(now=now)
            logger.warning(
                "[summary.scheduler] parent tick fallback done -> individual PUSH tick hhmm=%02d:%02d",
                now.hour,
                now.minute,
            )
        except Exception:
            logger.exception("[summary.scheduler] parent tick fallback failed")

    logger.info(
        "[summary.scheduler] parent tick finished hhmm=%02d:%02d "
        "ranking_enabled=%s run_entry=%s unified_ok=%s unified_timeout=%s elapsed=%.3fs fallback_state=%s",
        now.hour,
        now.minute,
        ranking_enabled,
        run_entry,
        unified_ok,
        unified_timeout,
        time.perf_counter() - t0,
        _push_fallback_state_snapshot(),
    )


# ============================================================
# public registration APIs
# ============================================================

def register_push_summary_tasks() -> None:
    try:
        _clear_tag(TAG_PUSH)
        schedule.every().minute.at(":00").do(_run_push_summary_tick).tag(TAG_PUSH)
        logger.info(
            "[summary.scheduler] registered PUSH summary every minute at :00 "
            "(1m always, 3m on 00/03/06..., 5m on 00/05/10..., run_entry=%s) tag=%s",
            _summary_entry_enabled(),
            TAG_PUSH,
        )
        _log_schedule_snapshot("after register_push_summary_tasks")
    except Exception:
        logger.exception("[summary.scheduler] register_push_summary_tasks failed")


def register_ranking_summary_tasks() -> None:
    try:
        _clear_tag(TAG_RANKING)
        schedule.every().minute.at(":00").do(_run_ranking_summary_tick).tag(TAG_RANKING)
        logger.info(
            "[summary.scheduler] registered RANKING summary every minute at :00 "
            "(1m always, 3m on 00/03/06..., 5m on 00/05/10...) tag=%s",
            TAG_RANKING,
        )
        _log_schedule_snapshot("after register_ranking_summary_tasks")
    except Exception:
        logger.exception("[summary.scheduler] register_ranking_summary_tasks failed")


def register_time_locked_summary_tasks() -> None:
    try:
        _clear_tag(TAG_PARENT)
        schedule.every().minute.at(":00").do(_run_summary_tick).tag(TAG_PARENT)
        logger.info(
            "[summary.scheduler] registered unified summary parent tick every minute at :00 "
            "(base=00, 1m always, 3m on 00/03/06..., 5m on 00/05/10..., "
            "ranking_enabled=%s, run_entry=%s, debug=%s, fallback=%s, timeout_guard=%s, "
            "parent_timeout=%.1f, child_timeout=%.1f, push_fallback_when_blocked=%s, "
            "fallback_async=%s, fallback_stale=%.1f) tag=%s",
            _ranking_enabled(),
            _summary_entry_enabled(),
            _summary_debug_enabled(),
            _fallback_individual_enabled(),
            _timeout_guard_enabled(),
            _parent_timeout_sec(),
            _child_timeout_sec(),
            _push_fallback_when_blocked_enabled(),
            _push_fallback_async_enabled(),
            _push_fallback_stale_sec(),
            TAG_PARENT,
        )
        _log_schedule_snapshot("after register_time_locked_summary_tasks")
    except Exception:
        logger.exception("[summary.scheduler] register_time_locked_summary_tasks failed")


def register_summary_tasks() -> None:
    try:
        register_time_locked_summary_tasks()
        logger.info(
            "[summary.scheduler] register_summary_tasks finished "
            "ranking_enabled=%s run_entry=%s debug=%s fallback=%s timeout_guard=%s "
            "parent_timeout=%.1f child_timeout=%.1f push_fallback_when_blocked=%s "
            "fallback_async=%s fallback_stale=%.1f",
            _ranking_enabled(),
            _summary_entry_enabled(),
            _summary_debug_enabled(),
            _fallback_individual_enabled(),
            _timeout_guard_enabled(),
            _parent_timeout_sec(),
            _child_timeout_sec(),
            _push_fallback_when_blocked_enabled(),
            _push_fallback_async_enabled(),
            _push_fallback_stale_sec(),
        )
    except Exception:
        logger.exception("[summary.scheduler] register_summary_tasks failed")


# ============================================================
# manual / debug entry
# ============================================================

def run_summary_tick_once(now: Optional[dt.datetime] = None) -> None:
    try:
        logger.info(
            "[summary.scheduler] run_summary_tick_once called ranking_enabled=%s run_entry=%s "
            "debug=%s fallback=%s timeout_guard=%s push_fallback_when_blocked=%s "
            "fallback_async=%s fallback_stale=%.1f fallback_state=%s",
            _ranking_enabled(),
            _summary_entry_enabled(),
            _summary_debug_enabled(),
            _fallback_individual_enabled(),
            _timeout_guard_enabled(),
            _push_fallback_when_blocked_enabled(),
            _push_fallback_async_enabled(),
            _push_fallback_stale_sec(),
            _push_fallback_state_snapshot(),
        )
        _run_summary_tick(now=now)
    except Exception:
        logger.exception("[summary.scheduler] run_summary_tick_once failed")


__all__ = [
    "should_run_1m",
    "should_run_3m",
    "should_run_5m",
    "register_push_summary_tasks",
    "register_ranking_summary_tasks",
    "register_time_locked_summary_tasks",
    "register_summary_tasks",
    "run_summary_tick_once",
]