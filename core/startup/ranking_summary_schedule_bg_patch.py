# ============================================================
# File   : core/startup/ranking_summary_schedule_bg_patch.py
# Version: V5-RANKING-SUMMARY-BG-TIMEOUT-COOLDOWN
# ------------------------------------------------------------
# 目的:
#   ranking_summary_all の schedule job が長時間 running のまま残り、
#   毎分 bg_still_running でスキップされ続ける問題を抑える。
#
# V5:
#   - BG実行が一定秒数を超えたら timeout 扱いにして cooldown へ入れる
#   - timeout/cooldown中は新しいBGを投入しない
#   - 古いBGスレッド自体はPython上killできないため、scheduler側だけ即復帰させる
#   - 既定では main.py 側のランキングサマリーBGを重くしない
#
# V4:
#   - V3の再帰を解消
#   - schedule登録関数は fast_startup_runtime_patch を経由しない
#   - _scheduler_bootstrap_job_wrapper はランキング本体を直接BG投入して即return
#   - scheduler_bootstrap._RANKING_JOB_RUNNING がstaleなら入口で解除
#   - BG内で job_ranking_summary_all と announce を直接呼ぶ
#
# ENV:
#   RANKING_SUMMARY_SCHEDULE_BG=1
#   RANKING_SUMMARY_BG_TIMEOUT_SEC=55
#   RANKING_SUMMARY_BG_COOLDOWN_SEC=180
#   RANKING_SUMMARY_BG_STALE_SEC=120
#   RANKING_SUMMARY_INTERNAL_STALE_SEC=120
# ============================================================

from __future__ import annotations

import datetime as dt
import functools
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

logger = logging.getLogger(__name__)
_PATCHED = False
_EXECUTOR: ThreadPoolExecutor | None = None
_LOCK = threading.RLock()
_RUNNING = False
_STARTED_AT: dt.datetime | None = None
_COOLDOWN_UNTIL: dt.datetime | None = None
_TIMEOUT_STREAK = 0
_ORIGINAL_SB = None


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        raw = os.getenv(name)
        if raw is None or str(raw).strip() == "":
            return bool(default)
        s = str(raw).strip().lower()
        if s in {"1", "true", "yes", "y", "on", "enable", "enabled"}:
            return True
        if s in {"0", "false", "no", "n", "off", "disable", "disabled"}:
            return False
    except Exception:
        pass
    return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        raw = os.getenv(name)
        if raw is not None and str(raw).strip() != "":
            return max(1.0, float(raw))
    except Exception:
        pass
    return float(default)


def _executor() -> ThreadPoolExecutor:
    global _EXECUTOR
    if _EXECUTOR is None:
        _EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ranking-summary-bg")
    return _EXECUTOR


def _elapsed_sec() -> float:
    try:
        if _STARTED_AT is None:
            return 0.0
        return max(0.0, (dt.datetime.now() - _STARTED_AT).total_seconds())
    except Exception:
        return 0.0


def _cooldown_seconds() -> float:
    try:
        base = _env_float("RANKING_SUMMARY_BG_COOLDOWN_SEC", 180.0)
        max_sec = _env_float("RANKING_SUMMARY_BG_COOLDOWN_MAX_SEC", 600.0)
        streak = max(1, int(_TIMEOUT_STREAK or 1))
        return min(max_sec, base * streak)
    except Exception:
        return 180.0


def _enter_cooldown(reason: str, *, elapsed: float) -> None:
    global _RUNNING, _STARTED_AT, _COOLDOWN_UNTIL, _TIMEOUT_STREAK
    try:
        _TIMEOUT_STREAK += 1
        cool_sec = _cooldown_seconds()
        _COOLDOWN_UNTIL = dt.datetime.now() + dt.timedelta(seconds=cool_sec)
        _RUNNING = False
        _STARTED_AT = None
        _set_scheduler_bootstrap_running(False)
        logger.warning(
            "[RANKING SUMMARY BG PATCH] timeout cooldown reason=%s elapsed=%.3fs timeout_streak=%s cooldown_sec=%.1f until=%s",
            reason,
            elapsed,
            _TIMEOUT_STREAK,
            cool_sec,
            _COOLDOWN_UNTIL,
        )
    except Exception:
        logger.debug("[RANKING SUMMARY BG PATCH] enter cooldown failed", exc_info=True)


def _check_timeout_or_cooldown_locked() -> bool:
    """Trueなら今回submitしない。_LOCK内から呼ぶ。"""
    global _COOLDOWN_UNTIL
    try:
        now = dt.datetime.now()
        if _COOLDOWN_UNTIL is not None and now < _COOLDOWN_UNTIL:
            remain = (_COOLDOWN_UNTIL - now).total_seconds()
            logger.warning(
                "[RANKING SUMMARY BG PATCH] submit skipped reason=timeout_cooldown remain=%.1fs until=%s timeout_streak=%s",
                remain,
                _COOLDOWN_UNTIL,
                _TIMEOUT_STREAK,
            )
            return True
        if _COOLDOWN_UNTIL is not None and now >= _COOLDOWN_UNTIL:
            logger.warning("[RANKING SUMMARY BG PATCH] cooldown expired until=%s", _COOLDOWN_UNTIL)
            _COOLDOWN_UNTIL = None

        if _RUNNING:
            elapsed = _elapsed_sec()
            timeout_sec = _env_float("RANKING_SUMMARY_BG_TIMEOUT_SEC", 55.0)
            if elapsed >= timeout_sec:
                _enter_cooldown("bg_timeout", elapsed=elapsed)
                return True
            logger.warning("[RANKING SUMMARY BG PATCH] submit skipped reason=bg_still_running elapsed=%.3fs timeout=%.1fs", elapsed, timeout_sec)
            return True
        return False
    except Exception:
        logger.debug("[RANKING SUMMARY BG PATCH] timeout/cooldown check failed", exc_info=True)
        return False


def _clear_bg_if_stale() -> bool:
    global _RUNNING, _STARTED_AT
    try:
        if not _RUNNING:
            return False
        elapsed = _elapsed_sec()
        stale = _env_float("RANKING_SUMMARY_BG_STALE_SEC", 120.0)
        if elapsed < stale:
            return False
        _RUNNING = False
        _STARTED_AT = None
        logger.warning("[RANKING SUMMARY BG PATCH] bg stale running cleared elapsed=%.3fs stale=%.3fs", elapsed, stale)
        return True
    except Exception:
        logger.debug("[RANKING SUMMARY BG PATCH] bg stale clear failed", exc_info=True)
        return False


def _clear_scheduler_bootstrap_internal_if_stale(*, force: bool = False, reason: str = "stale_check") -> bool:
    try:
        import core.startup.scheduler_bootstrap as sb
        running = bool(getattr(sb, "_RANKING_JOB_RUNNING", False))
        if not running:
            return False
        started_at = getattr(sb, "_RANKING_JOB_STARTED_AT", None)
        elapsed = 999999.0
        if isinstance(started_at, dt.datetime):
            elapsed = max(0.0, (dt.datetime.now() - started_at).total_seconds())
        stale = _env_float("RANKING_SUMMARY_INTERNAL_STALE_SEC", 120.0)
        if (not force) and elapsed < stale:
            return False
        lock = getattr(sb, "_RANKING_JOB_LOCK", None)
        if lock is not None:
            with lock:
                sb._RANKING_JOB_RUNNING = False
                sb._RANKING_JOB_STARTED_AT = None
        else:
            sb._RANKING_JOB_RUNNING = False
            sb._RANKING_JOB_STARTED_AT = None
        try:
            sb._set_global_attr("ranking_summary_job_running", False)
            sb._set_global_attr("ranking_summary_job_stale_cleared_at", dt.datetime.now())
            sb._set_global_attr("ranking_summary_job_stale_clear_reason", reason)
        except Exception:
            pass
        logger.warning(
            "[RANKING SUMMARY BG PATCH] scheduler_bootstrap internal running stale cleared reason=%s started_at=%s elapsed=%.3fs stale=%.3fs force=%s",
            reason, started_at, elapsed, stale, force,
        )
        return True
    except Exception:
        logger.debug("[RANKING SUMMARY BG PATCH] scheduler_bootstrap internal stale clear failed", exc_info=True)
        return False


def _set_scheduler_bootstrap_running(value: bool, *, started_at: dt.datetime | None = None) -> None:
    try:
        import core.startup.scheduler_bootstrap as sb
        lock = getattr(sb, "_RANKING_JOB_LOCK", None)
        if lock is not None:
            with lock:
                sb._RANKING_JOB_RUNNING = bool(value)
                sb._RANKING_JOB_STARTED_AT = started_at if value else None
        else:
            sb._RANKING_JOB_RUNNING = bool(value)
            sb._RANKING_JOB_STARTED_AT = started_at if value else None
        try:
            sb._set_global_attr("ranking_summary_job_running", bool(value))
            if value:
                sb._set_global_attr("ranking_summary_job_started_at", started_at)
            else:
                sb._set_global_attr("ranking_summary_job_finished_at", dt.datetime.now())
        except Exception:
            pass
    except Exception:
        logger.debug("[RANKING SUMMARY BG PATCH] set scheduler_bootstrap running failed", exc_info=True)


def _run_core_ranking_summary_job() -> Any:
    import core.startup.scheduler_bootstrap as sb

    started = dt.datetime.now()
    perf_started = time.perf_counter()
    _set_scheduler_bootstrap_running(True, started_at=started)
    try:
        logger.warning("[RANKING SUMMARY BG PATCH] core ranking job start at=%s", started.strftime("%Y-%m-%d %H:%M:%S"))
        fn = sb._resolve_attr(
            "scheduler_jobs.summary.ranking_summary_jobs",
            "job_ranking_summary_all",
            quiet=False,
        )
        if not callable(fn):
            logger.warning("[RANKING SUMMARY BG PATCH] core ranking job skipped function unavailable")
            return None
        kwargs = sb._build_ranking_job_kwargs(force=False)
        result = sb._call_with_supported_kwargs(fn, **kwargs)
        elapsed = time.perf_counter() - perf_started
        try:
            sb._set_global_attr("last_ranking_summary_job_at", dt.datetime.now())
            sb._set_global_attr("last_ranking_summary_job_result", sb._summarize_result(result))
        except Exception:
            pass
        logger.warning("[RANKING SUMMARY BG PATCH] core ranking job done elapsed=%.3fs result=%s", elapsed, sb._summarize_result(result))
        try:
            announce_results = sb._announce_ranking_summary_intervals_safe(top_n=10, use_discord=True, intervals=(1, 3, 5))
            sb._set_global_attr("last_ranking_summary_announce_results", announce_results)
            logger.warning("[RANKING SUMMARY BG PATCH] announce done results=%s", announce_results)
        except Exception:
            logger.exception("[RANKING SUMMARY BG PATCH] announce failed")
        return result
    except Exception:
        logger.exception("[RANKING SUMMARY BG PATCH] core ranking job failed")
        return None
    finally:
        _set_scheduler_bootstrap_running(False)


def _bg_task(started_at: dt.datetime) -> None:
    global _RUNNING, _STARTED_AT, _TIMEOUT_STREAK
    t0 = time.perf_counter()
    try:
        _clear_scheduler_bootstrap_internal_if_stale(reason="bg_task_before_core")
        logger.warning("[RANKING SUMMARY BG PATCH] bg start started_at=%s", started_at)
        _run_core_ranking_summary_job()
        elapsed = time.perf_counter() - t0
        _TIMEOUT_STREAK = 0
        logger.warning("[RANKING SUMMARY BG PATCH] bg done elapsed=%.3fs", elapsed)
    except Exception:
        logger.exception("[RANKING SUMMARY BG PATCH] bg failed elapsed=%.3fs", time.perf_counter() - t0)
    finally:
        with _LOCK:
            _RUNNING = False
            _STARTED_AT = None


def _scheduler_bootstrap_job_wrapper(*args: Any, **kwargs: Any):
    global _RUNNING, _STARTED_AT
    _clear_scheduler_bootstrap_internal_if_stale(reason="direct_wrapper_entry")
    if not _env_bool("RANKING_SUMMARY_SCHEDULE_BG", True):
        return _run_core_ranking_summary_job()

    with _LOCK:
        _clear_bg_if_stale()
        if _check_timeout_or_cooldown_locked():
            return None
        _RUNNING = True
        _STARTED_AT = dt.datetime.now()
        started = _STARTED_AT

    _executor().submit(_bg_task, started)
    logger.warning("[RANKING SUMMARY BG PATCH] submitted schedule job returns immediately started_at=%s", started)
    return None


def _replace_schedule_job_refs(old: Callable, new: Callable) -> int:
    changed = 0
    try:
        import schedule
        for job in list(getattr(schedule, "jobs", []) or []):
            try:
                tags = getattr(job, "tags", set()) or set()
                if "ranking_summary_all" not in tags:
                    continue
                jf = getattr(job, "job_func", None)
                if isinstance(jf, functools.partial):
                    current = getattr(jf, "func", None)
                    if current is old or getattr(current, "__name__", "") in {"_scheduler_bootstrap_job_wrapper", "_ranking_job_safe_no_return", "_run_ranking_summary_all_job_safe"}:
                        job.job_func = functools.partial(new, *jf.args, **(jf.keywords or {}))
                        changed += 1
                elif jf is old or getattr(jf, "__name__", "") in {"_scheduler_bootstrap_job_wrapper", "_ranking_job_safe_no_return", "_run_ranking_summary_all_job_safe"}:
                    job.job_func = new
                    changed += 1
            except Exception:
                logger.debug("[RANKING SUMMARY BG PATCH] schedule job ref replace skipped", exc_info=True)
    except Exception:
        logger.debug("[RANKING SUMMARY BG PATCH] schedule job ref replace failed", exc_info=True)
    return changed


def _patch_scheduler_bootstrap_direct() -> bool:
    global _ORIGINAL_SB
    try:
        import core.startup.scheduler_bootstrap as sb
        cur = getattr(sb, "_run_ranking_summary_all_job_safe", None)
        if not callable(cur):
            logger.warning("[RANKING SUMMARY BG PATCH] scheduler_bootstrap direct target not callable")
            return False
        if not getattr(cur, "_ranking_summary_direct_bg_v5", False):
            _ORIGINAL_SB = cur
            _scheduler_bootstrap_job_wrapper._ranking_summary_direct_bg_v5 = True  # type: ignore[attr-defined]
            _scheduler_bootstrap_job_wrapper._original = cur  # type: ignore[attr-defined]
            sb._run_ranking_summary_all_job_safe = _scheduler_bootstrap_job_wrapper
        changed = _replace_schedule_job_refs(cur, _scheduler_bootstrap_job_wrapper)
        logger.warning("[RANKING SUMMARY BG PATCH] scheduler_bootstrap direct BG wrapper installed schedule_refs_changed=%s", changed)
        _clear_scheduler_bootstrap_internal_if_stale(reason="direct_patch_install")
        return True
    except Exception:
        logger.exception("[RANKING SUMMARY BG PATCH] scheduler_bootstrap direct patch failed")
        return False


def install() -> bool:
    global _PATCHED
    direct_ok = _patch_scheduler_bootstrap_direct()
    _clear_scheduler_bootstrap_internal_if_stale(reason="install_final")
    _PATCHED = bool(direct_ok)
    logger.warning(
        "[RANKING SUMMARY BG PATCH] installed V5 enabled=%s bg_timeout=%.1f cooldown=%.1f bg_stale_sec=%.1f internal_stale_sec=%.1f direct_ok=%s",
        _env_bool("RANKING_SUMMARY_SCHEDULE_BG", True),
        _env_float("RANKING_SUMMARY_BG_TIMEOUT_SEC", 55.0),
        _env_float("RANKING_SUMMARY_BG_COOLDOWN_SEC", 180.0),
        _env_float("RANKING_SUMMARY_BG_STALE_SEC", 120.0),
        _env_float("RANKING_SUMMARY_INTERNAL_STALE_SEC", 120.0),
        direct_ok,
    )
    return bool(direct_ok)


try:
    install()
except Exception:
    logger.exception("[RANKING SUMMARY BG PATCH] auto install failed")


__all__ = ["install"]
