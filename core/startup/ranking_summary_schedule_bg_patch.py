# ============================================================
# File   : core/startup/ranking_summary_schedule_bg_patch.py
# Version: V3-RANKING-SUMMARY-DIRECT-SCHEDULERBOOTSTRAP-WRAP
# ------------------------------------------------------------
# 目的:
#   ranking_summary_all の schedule job が数分間 running のまま残り、
#   previous still running / internal_previous_still_running で
#   スキップされ続ける問題を防ぐ。
#
# V3:
#   - fast_startup_runtime_patch 側だけでなく、
#     scheduler_bootstrap._run_ranking_summary_all_job_safe 自体を直接ラップ
#   - schedule に登録済みの関数参照も、可能なら job_func.func を差し替える
#   - _RANKING_JOB_RUNNING が stale 秒数以上残っていたら、入口で解除してから実行
#   - 09:18 started_at が 09:23 以降も残る internal_previous_still_running を防止
#
# ENV:
#   RANKING_SUMMARY_SCHEDULE_BG=1
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
_ORIGINAL_FAST = None
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
    """scheduler_bootstrap 内部の _RANKING_JOB_RUNNING が古く残るケースを解除する。"""
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
            reason,
            started_at,
            elapsed,
            stale,
            force,
        )
        return True
    except Exception:
        logger.debug("[RANKING SUMMARY BG PATCH] scheduler_bootstrap internal stale clear failed", exc_info=True)
        return False


def _task(original: Callable, args: tuple[Any, ...], kwargs: dict[str, Any], started_at: dt.datetime) -> None:
    global _RUNNING, _STARTED_AT
    t0 = time.perf_counter()
    try:
        _clear_scheduler_bootstrap_internal_if_stale(reason="bg_task_before_original")
        logger.warning("[RANKING SUMMARY BG PATCH] bg start started_at=%s", started_at)
        ret = original(*args, **kwargs)
        logger.warning("[RANKING SUMMARY BG PATCH] bg done elapsed=%.3fs ret_type=%s", time.perf_counter() - t0, type(ret).__name__)
    except Exception:
        logger.exception("[RANKING SUMMARY BG PATCH] bg failed elapsed=%.3fs", time.perf_counter() - t0)
    finally:
        with _LOCK:
            _RUNNING = False
            _STARTED_AT = None


def _ranking_job_safe_no_return_bg(*args: Any, **kwargs: Any):
    """fast_startup_runtime_patch 用のBGラッパ。"""
    global _RUNNING, _STARTED_AT
    if not _env_bool("RANKING_SUMMARY_SCHEDULE_BG", True):
        _clear_scheduler_bootstrap_internal_if_stale(reason="fast_wrapper_sync_mode")
        if callable(_ORIGINAL_FAST):
            return _ORIGINAL_FAST(*args, **kwargs)
        return None

    original = _ORIGINAL_FAST
    if not callable(original):
        logger.warning("[RANKING SUMMARY BG PATCH] original fast ranking job not callable")
        return None

    with _LOCK:
        _clear_bg_if_stale()
        _clear_scheduler_bootstrap_internal_if_stale(reason="fast_wrapper_submit")
        if _RUNNING:
            logger.warning(
                "[RANKING SUMMARY BG PATCH] submit skipped reason=bg_still_running elapsed=%.3fs",
                _elapsed_sec(),
            )
            return None
        _RUNNING = True
        _STARTED_AT = dt.datetime.now()
        started = _STARTED_AT

    _executor().submit(_task, original, tuple(args), dict(kwargs), started)
    logger.warning("[RANKING SUMMARY BG PATCH] submitted schedule job returns immediately started_at=%s", started)
    return None


def _scheduler_bootstrap_job_wrapper(*args: Any, **kwargs: Any):
    """scheduler_bootstrap._run_ranking_summary_all_job_safe の入口で stale を直接解除する。"""
    _clear_scheduler_bootstrap_internal_if_stale(reason="direct_scheduler_bootstrap_wrapper_entry")
    if callable(_ORIGINAL_SB):
        return _ORIGINAL_SB(*args, **kwargs)
    return None


def _replace_schedule_job_refs(old: Callable, new: Callable) -> int:
    """既にscheduleへ登録済みのjob_func.funcがoldならnewへ差し替える。"""
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
                    if getattr(jf, "func", None) is old:
                        job.job_func = functools.partial(new, *jf.args, **(jf.keywords or {}))
                        changed += 1
                elif jf is old:
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
        if getattr(cur, "_ranking_summary_direct_stale_wrapper_v3", False):
            _clear_scheduler_bootstrap_internal_if_stale(reason="direct_already_patched")
            return True

        _ORIGINAL_SB = cur
        _scheduler_bootstrap_job_wrapper._ranking_summary_direct_stale_wrapper_v3 = True  # type: ignore[attr-defined]
        _scheduler_bootstrap_job_wrapper._original = cur  # type: ignore[attr-defined]
        sb._run_ranking_summary_all_job_safe = _scheduler_bootstrap_job_wrapper
        changed = _replace_schedule_job_refs(cur, _scheduler_bootstrap_job_wrapper)
        logger.warning("[RANKING SUMMARY BG PATCH] scheduler_bootstrap direct wrapper installed schedule_refs_changed=%s", changed)
        _clear_scheduler_bootstrap_internal_if_stale(reason="direct_patch_install")
        return True
    except Exception:
        logger.exception("[RANKING SUMMARY BG PATCH] scheduler_bootstrap direct patch failed")
        return False


def install() -> bool:
    global _PATCHED, _ORIGINAL_FAST
    direct_ok = _patch_scheduler_bootstrap_direct()

    if _PATCHED:
        _clear_scheduler_bootstrap_internal_if_stale(reason="install_already_patched")
        return bool(direct_ok)

    try:
        import core.startup.fast_startup_runtime_patch as fast_patch
    except Exception:
        logger.exception("[RANKING SUMMARY BG PATCH] fast_startup_runtime_patch import failed")
        _PATCHED = bool(direct_ok)
        return bool(direct_ok)

    try:
        cur = getattr(fast_patch, "_ranking_job_safe_no_return", None)
        if not callable(cur):
            logger.warning("[RANKING SUMMARY BG PATCH] target _ranking_job_safe_no_return not callable")
            _PATCHED = bool(direct_ok)
            return bool(direct_ok)

        if not getattr(cur, "_ranking_summary_bg_patch_v3", False):
            _ORIGINAL_FAST = cur
            _ranking_job_safe_no_return_bg._ranking_summary_bg_patch_v3 = True  # type: ignore[attr-defined]
            _ranking_job_safe_no_return_bg._original = cur  # type: ignore[attr-defined]
            fast_patch._ranking_job_safe_no_return = _ranking_job_safe_no_return_bg

        _clear_scheduler_bootstrap_internal_if_stale(reason="install_final")
        _PATCHED = True
        logger.warning(
            "[RANKING SUMMARY BG PATCH] installed V3 enabled=%s bg_stale_sec=%.1f internal_stale_sec=%.1f direct_ok=%s",
            _env_bool("RANKING_SUMMARY_SCHEDULE_BG", True),
            _env_float("RANKING_SUMMARY_BG_STALE_SEC", 120.0),
            _env_float("RANKING_SUMMARY_INTERNAL_STALE_SEC", 120.0),
            direct_ok,
        )
        return True
    except Exception:
        logger.exception("[RANKING SUMMARY BG PATCH] install failed")
        _PATCHED = bool(direct_ok)
        return bool(direct_ok)


try:
    install()
except Exception:
    logger.exception("[RANKING SUMMARY BG PATCH] auto install failed")


__all__ = ["install"]
