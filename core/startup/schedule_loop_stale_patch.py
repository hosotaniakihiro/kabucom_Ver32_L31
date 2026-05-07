# ============================================================
# File   : core/startup/schedule_loop_stale_patch.py
# Version: PRODUCTION-STABLE-SCHEDULE-LOOP-STALE-GUARD-V1
# ------------------------------------------------------------
# Purpose:
#   schedule_loop.py 本体を壊さず、長時間残った running job を stale として解除する。
#
# Why:
#   ranking_summary_all が300秒以上 previous still running になり、
#   次のランキングサマリー/AI判定を止め続けるケースを防ぐ。
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.environ.get(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _is_ranking_summary_key(key: str) -> bool:
    s = str(key or "")
    return "ranking_summary_all" in s or "_run_ranking_summary_all_job_safe" in s


def install_schedule_loop_stale_patch() -> bool:
    try:
        from core.startup import schedule_loop as target
    except Exception:
        logger.exception("[SCHEDULE LOOP STALE PATCH] import target failed")
        return False

    if getattr(target, "_stale_guard_patch_installed", False):
        return True

    orig_dispatch = getattr(target, "_dispatch_due_job", None)
    if not callable(orig_dispatch):
        logger.warning("[SCHEDULE LOOP STALE PATCH] _dispatch_due_job not found")
        return False

    stale_sec_default = _env_float("SCHEDULE_LOOP_STALE_JOB_SEC", 240.0)
    ranking_stale_sec = _env_float("RANKING_SUMMARY_STALE_JOB_SEC", 180.0)

    def _clear_if_stale(job: Any, key: str) -> bool:
        try:
            running_jobs = getattr(target, "_RUNNING_JOBS", {})
            running_lock = getattr(target, "_RUNNING_JOBS_LOCK", None)
            now = dt.datetime.now()
            max_sec = ranking_stale_sec if _is_ranking_summary_key(key) else stale_sec_default

            if running_lock is None:
                meta = running_jobs.get(key)
                if not meta:
                    return False
                started_at = meta.get("started_at")
                if isinstance(started_at, dt.datetime):
                    elapsed = max(0.0, (now - started_at).total_seconds())
                    if elapsed >= max_sec:
                        running_jobs.pop(key, None)
                        logger.warning(
                            "[SCHEDULE LOOP STALE PATCH] cleared stale running job key=%s elapsed=%.1fs max=%.1fs",
                            key,
                            elapsed,
                            max_sec,
                        )
                        return True
                return False

            with running_lock:
                meta = running_jobs.get(key)
                if not meta:
                    return False
                started_at = meta.get("started_at")
                if not isinstance(started_at, dt.datetime):
                    return False
                elapsed = max(0.0, (now - started_at).total_seconds())
                if elapsed < max_sec:
                    return False
                running_jobs.pop(key, None)

            logger.warning(
                "[SCHEDULE LOOP STALE PATCH] cleared stale running job key=%s elapsed=%.1fs max=%.1fs job=%s",
                key,
                elapsed,
                max_sec,
                repr(job)[:300],
            )
            return True
        except Exception:
            logger.debug("[SCHEDULE LOOP STALE PATCH] stale clear failed key=%s", key, exc_info=True)
            return False

    def dispatch_due_job_with_stale_guard(job: Any, *args: Any, **kwargs: Any) -> bool:
        try:
            key_fn = getattr(target, "_job_key", None)
            key = key_fn(job) if callable(key_fn) else repr(job)
            _clear_if_stale(job, key)
        except Exception:
            pass
        return bool(orig_dispatch(job, *args, **kwargs))

    target._dispatch_due_job = dispatch_due_job_with_stale_guard
    target._stale_guard_patch_installed = True

    logger.warning(
        "[SCHEDULE LOOP STALE PATCH] installed stale_sec=%.1fs ranking_stale_sec=%.1fs",
        stale_sec_default,
        ranking_stale_sec,
    )
    return True


try:
    install_schedule_loop_stale_patch()
except Exception:
    logger.exception("[SCHEDULE LOOP STALE PATCH] auto install failed")


__all__ = ["install_schedule_loop_stale_patch"]
