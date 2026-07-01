# ============================================================
# File   : core/startup/summary_scheduler_shutdown_guard_patch.py
# Purpose:
#   - Python interpreter shutdown 中に summary scheduler が
#     ThreadPoolExecutor.submit() へ新規投入して落ちる問題を防ぐ
#   - RuntimeError: cannot schedule new futures after interpreter shutdown
#     を安全に skip する
# ============================================================

from __future__ import annotations

import atexit
import importlib
import logging
import sys
import threading
from functools import wraps
from typing import Any, Callable

logger = logging.getLogger(__name__)

_PATCH_LOCK = threading.RLock()
_SHUTDOWN = threading.Event()
_INSTALLED = False


def _mark_shutdown() -> None:
    try:
        _SHUTDOWN.set()
    except Exception:
        pass


def _is_shutdown() -> bool:
    try:
        if _SHUTDOWN.is_set():
            return True
        is_finalizing = getattr(sys, "is_finalizing", None)
        if callable(is_finalizing) and is_finalizing():
            return True
    except Exception:
        return True
    return False


def _is_executor_shutdown_runtime_error(exc: BaseException) -> bool:
    try:
        msg = str(exc)
    except Exception:
        return False
    return (
        "cannot schedule new futures after interpreter shutdown" in msg
        or "cannot schedule new futures after shutdown" in msg
    )


def _wrap_tick(fn: Callable[..., Any], *, name: str) -> Callable[..., Any]:
    @wraps(fn)
    def _guarded(*args: Any, **kwargs: Any) -> Any:
        if _is_shutdown():
            try:
                logger.warning("[SUMMARY SCHEDULER SHUTDOWN GUARD] skip tick during shutdown name=%s", name)
            except Exception:
                pass
            return None
        try:
            return fn(*args, **kwargs)
        except RuntimeError as e:
            if _is_executor_shutdown_runtime_error(e):
                try:
                    logger.warning(
                        "[SUMMARY SCHEDULER SHUTDOWN GUARD] suppress RuntimeError during shutdown name=%s error=%s",
                        name,
                        e,
                    )
                except Exception:
                    pass
                return None
            raise

    return _guarded


def install() -> bool:
    """Install shutdown-safe wrappers into scheduler_jobs.summary.scheduler."""
    global _INSTALLED

    with _PATCH_LOCK:
        if _INSTALLED:
            return True

        try:
            atexit.register(_mark_shutdown)
        except Exception:
            pass

        try:
            scheduler = importlib.import_module("scheduler_jobs.summary.scheduler")
        except Exception:
            logger.exception("[SUMMARY SCHEDULER SHUTDOWN GUARD] scheduler import failed")
            return False

        try:
            if getattr(scheduler, "_SUMMARY_SCHEDULER_SHUTDOWN_GUARD_INSTALLED", False):
                _INSTALLED = True
                return True

            original_run_with_timeout = getattr(scheduler, "_run_with_timeout", None)
            if callable(original_run_with_timeout):

                @wraps(original_run_with_timeout)
                def _run_with_timeout_guarded(*args: Any, **kwargs: Any):
                    label = kwargs.get("label", "")
                    if _is_shutdown():
                        try:
                            logger.warning(
                                "[SUMMARY SCHEDULER SHUTDOWN GUARD] skip _run_with_timeout during shutdown label=%s",
                                label,
                            )
                        except Exception:
                            pass
                        return False, False, None
                    try:
                        return original_run_with_timeout(*args, **kwargs)
                    except RuntimeError as e:
                        if _is_executor_shutdown_runtime_error(e):
                            try:
                                logger.warning(
                                    "[SUMMARY SCHEDULER SHUTDOWN GUARD] suppress executor submit RuntimeError label=%s error=%s",
                                    label,
                                    e,
                                )
                            except Exception:
                                pass
                            return False, False, None
                        raise

                scheduler._run_with_timeout = _run_with_timeout_guarded

            original_invoke_job = getattr(scheduler, "_invoke_job", None)
            if callable(original_invoke_job):
                scheduler._invoke_job = _wrap_tick(original_invoke_job, name="_invoke_job")

            for name in (
                "_run_push_summary_tick",
                "_run_ranking_summary_tick",
                "_run_summary_tick",
                "_run_push_fallback_when_unified_blocked",
                "_push_fallback_worker",
                "run_summary_tick_once",
            ):
                fn = getattr(scheduler, name, None)
                if callable(fn):
                    setattr(scheduler, name, _wrap_tick(fn, name=name))

            setattr(scheduler, "_SUMMARY_SCHEDULER_SHUTDOWN_GUARD_INSTALLED", True)
            _INSTALLED = True
            logger.warning("[SUMMARY SCHEDULER SHUTDOWN GUARD] installed")
            return True

        except Exception:
            logger.exception("[SUMMARY SCHEDULER SHUTDOWN GUARD] install failed")
            return False
