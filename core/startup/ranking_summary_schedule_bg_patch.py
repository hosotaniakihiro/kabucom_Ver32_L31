# ============================================================
# File   : core/startup/ranking_summary_schedule_bg_patch.py
# Version: V2-RANKING-SUMMARY-BG-AND-INTERNAL-STALE-CLEAR
# ------------------------------------------------------------
# 目的:
#   ranking_summary_all の schedule job が数分間 running のまま残り、
#   毎分 previous still running / internal_previous_still_running で
#   スキップされ続ける問題を防ぐ。
#
# V2:
#   - schedule側だけでなく scheduler_bootstrap._RANKING_JOB_RUNNING も stale解除
#   - 12:09ログの started_at=12:01 elapsed=480s internal_previous_still_running を解除
#   - fast_startup_runtime_patch._ranking_job_safe_no_return を背景実行版へ差し替え
#   - schedule job 自体は即 return None して schedule_loop を詰まらせない
#
# ENV:
#   RANKING_SUMMARY_SCHEDULE_BG=1
#   RANKING_SUMMARY_BG_STALE_SEC=120
#   RANKING_SUMMARY_INTERNAL_STALE_SEC=120
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

logger = logging.getLogger(__name__)
_PATCHED = False
_EXECUTOR: ThreadPoolExecutor | None = None
_LOCK = threading.RLock()
_RUNNING = False
_STARTED_AT: dt.datetime | None = None
_ORIGINAL = None


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


def _clear_scheduler_bootstrap_internal_if_stale() -> bool:
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
        if elapsed < stale:
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
        except Exception:
            pass
        logger.warning(
            "[RANKING SUMMARY BG PATCH] scheduler_bootstrap internal running stale cleared started_at=%s elapsed=%.3fs stale=%.3fs",
            started_at,
            elapsed,
            stale,
        )
        return True
    except Exception:
        logger.debug("[RANKING SUMMARY BG PATCH] scheduler_bootstrap internal stale clear failed", exc_info=True)
        return False


def _task(original, args: tuple[Any, ...], kwargs: dict[str, Any], started_at: dt.datetime) -> None:
    global _RUNNING, _STARTED_AT
    t0 = time.perf_counter()
    try:
        _clear_scheduler_bootstrap_internal_if_stale()
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
    global _RUNNING, _STARTED_AT
    if not _env_bool("RANKING_SUMMARY_SCHEDULE_BG", True):
        _clear_scheduler_bootstrap_internal_if_stale()
        if callable(_ORIGINAL):
            return _ORIGINAL(*args, **kwargs)
        return None

    original = _ORIGINAL
    if not callable(original):
        logger.warning("[RANKING SUMMARY BG PATCH] original ranking job not callable")
        return None

    with _LOCK:
        _clear_bg_if_stale()
        _clear_scheduler_bootstrap_internal_if_stale()
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


def install() -> bool:
    global _PATCHED, _ORIGINAL
    if _PATCHED:
        _clear_scheduler_bootstrap_internal_if_stale()
        return True
    try:
        import core.startup.fast_startup_runtime_patch as fast_patch
    except Exception:
        logger.exception("[RANKING SUMMARY BG PATCH] fast_startup_runtime_patch import failed")
        return False

    try:
        cur = getattr(fast_patch, "_ranking_job_safe_no_return", None)
        if not callable(cur):
            logger.warning("[RANKING SUMMARY BG PATCH] target _ranking_job_safe_no_return not callable")
            return False
        if getattr(cur, "_ranking_summary_bg_patch_v2", False):
            _PATCHED = True
            _clear_scheduler_bootstrap_internal_if_stale()
            return True

        _ORIGINAL = cur
        _ranking_job_safe_no_return_bg._ranking_summary_bg_patch_v2 = True  # type: ignore[attr-defined]
        _ranking_job_safe_no_return_bg._original = cur  # type: ignore[attr-defined]
        fast_patch._ranking_job_safe_no_return = _ranking_job_safe_no_return_bg

        try:
            import core.startup.scheduler_bootstrap as sb
            if getattr(sb, "_run_ranking_summary_all_job_safe", None) is cur:
                sb._run_ranking_summary_all_job_safe = _ranking_job_safe_no_return_bg
        except Exception:
            logger.debug("[RANKING SUMMARY BG PATCH] scheduler_bootstrap ref patch skipped", exc_info=True)

        _clear_scheduler_bootstrap_internal_if_stale()
        _PATCHED = True
        logger.warning(
            "[RANKING SUMMARY BG PATCH] installed V2 enabled=%s bg_stale_sec=%.1f internal_stale_sec=%.1f",
            _env_bool("RANKING_SUMMARY_SCHEDULE_BG", True),
            _env_float("RANKING_SUMMARY_BG_STALE_SEC", 120.0),
            _env_float("RANKING_SUMMARY_INTERNAL_STALE_SEC", 120.0),
        )
        return True
    except Exception:
        logger.exception("[RANKING SUMMARY BG PATCH] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[RANKING SUMMARY BG PATCH] auto install failed")


__all__ = ["install"]
