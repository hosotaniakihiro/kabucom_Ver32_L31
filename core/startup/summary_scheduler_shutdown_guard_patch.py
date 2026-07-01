# ============================================================
# File   : core/startup/summary_scheduler_shutdown_guard_patch.py
# Version: V2-RECOVER-TIMEOUT-EXECUTOR
# ------------------------------------------------------------
# Purpose:
#   - Python interpreter shutdown 中に summary scheduler が
#     ThreadPoolExecutor.submit() へ新規投入して落ちる問題を防ぐ。
#   - 稼働中に scheduler._timeout_executor が shutdown 済みになった場合は、
#     suppress ではなく executor を再生成して再投入する。
#
# V2:
#   - ログ `cannot schedule new futures after shutdown` は interpreter shutdown だけでなく、
#     timeout guard の executor 自体が shutdown 済みになった場合にも出る。
#   - これを単に suppress すると PUSH 1m/3m summary が CALL failed になり、
#     summary_parent_tick timeout / PUSH stale / WS reconnect が連鎖する。
#   - sys.is_finalizing()==False なら runtime executor shutdown と判断し、
#     scheduler._timeout_executor を新規 ThreadPoolExecutor に差し替えて再実行する。
# ============================================================

from __future__ import annotations

import atexit
import importlib
import logging
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from functools import wraps
from typing import Any, Callable

logger = logging.getLogger(__name__)

_PATCH_LOCK = threading.RLock()
_SHUTDOWN = threading.Event()
_INSTALLED = False
_RECOVER_LOCK = threading.RLock()


def _mark_shutdown() -> None:
    try:
        _SHUTDOWN.set()
    except Exception:
        pass


def _is_interpreter_shutdown() -> bool:
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


def _safe_label_from_args(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
    try:
        return str(kwargs.get("label") or "")
    except Exception:
        return ""


def _reset_scheduler_timeout_executor(scheduler: Any, *, reason: str, label: str) -> bool:
    try:
        with _RECOVER_LOCK:
            old = getattr(scheduler, "_timeout_executor", None)
            try:
                if old is not None:
                    old.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass
            new_exec = ThreadPoolExecutor(max_workers=4, thread_name_prefix="summary-timeout-guard-recovered")
            setattr(scheduler, "_timeout_executor", new_exec)
            logger.warning(
                "[SUMMARY SCHEDULER SHUTDOWN GUARD] recovered scheduler._timeout_executor label=%s reason=%s new=%s",
                label,
                reason,
                new_exec,
            )
            return True
    except Exception:
        logger.exception("[SUMMARY SCHEDULER SHUTDOWN GUARD] executor recovery failed label=%s reason=%s", label, reason)
        return False


def _wrap_tick(fn: Callable[..., Any], *, name: str) -> Callable[..., Any]:
    @wraps(fn)
    def _guarded(*args: Any, **kwargs: Any) -> Any:
        if _is_interpreter_shutdown():
            try:
                logger.warning("[SUMMARY SCHEDULER SHUTDOWN GUARD] skip tick during interpreter shutdown name=%s", name)
            except Exception:
                pass
            return None
        try:
            return fn(*args, **kwargs)
        except RuntimeError as e:
            if _is_executor_shutdown_runtime_error(e):
                if _is_interpreter_shutdown():
                    try:
                        logger.warning(
                            "[SUMMARY SCHEDULER SHUTDOWN GUARD] suppress RuntimeError during interpreter shutdown name=%s error=%s",
                            name,
                            e,
                        )
                    except Exception:
                        pass
                    return None
                logger.warning(
                    "[SUMMARY SCHEDULER SHUTDOWN GUARD] RuntimeError outside interpreter shutdown name=%s error=%s -> propagate to timeout wrapper/retry",
                    name,
                    e,
                )
            raise

    return _guarded


def install() -> bool:
    """Install shutdown-safe and runtime-recovery wrappers into scheduler_jobs.summary.scheduler."""
    global _INSTALLED

    with _PATCH_LOCK:
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
            # V1 が既に入っていても、V2 は再適用する。
            if getattr(scheduler, "_SUMMARY_SCHEDULER_SHUTDOWN_GUARD_V2_INSTALLED", False):
                _INSTALLED = True
                return True

            original_run_with_timeout = getattr(scheduler, "_run_with_timeout", None)
            if callable(original_run_with_timeout):
                # 既存V1/V2 wrapper の下にある original をできるだけ辿る。
                base_run_with_timeout = getattr(original_run_with_timeout, "_summary_shutdown_guard_original", None) or original_run_with_timeout

                @wraps(base_run_with_timeout)
                def _run_with_timeout_guarded(*args: Any, **kwargs: Any):
                    label = _safe_label_from_args(args, kwargs)
                    if _is_interpreter_shutdown():
                        try:
                            logger.warning(
                                "[SUMMARY SCHEDULER SHUTDOWN GUARD] skip _run_with_timeout during interpreter shutdown label=%s",
                                label,
                            )
                        except Exception:
                            pass
                        return False, False, None
                    try:
                        return base_run_with_timeout(*args, **kwargs)
                    except RuntimeError as e:
                        if not _is_executor_shutdown_runtime_error(e):
                            raise
                        if _is_interpreter_shutdown():
                            try:
                                logger.warning(
                                    "[SUMMARY SCHEDULER SHUTDOWN GUARD] suppress executor submit RuntimeError during interpreter shutdown label=%s error=%s",
                                    label,
                                    e,
                                )
                            except Exception:
                                pass
                            return False, False, None

                        # 稼働中の executor shutdown は復旧対象。再生成して同じ job をもう一度投入する。
                        logger.warning(
                            "[SUMMARY SCHEDULER SHUTDOWN GUARD] executor submit RuntimeError outside interpreter shutdown label=%s error=%s -> recover/retry",
                            label,
                            e,
                        )
                        if _reset_scheduler_timeout_executor(scheduler, reason=str(e), label=label):
                            try:
                                return base_run_with_timeout(*args, **kwargs)
                            except RuntimeError as e2:
                                if _is_executor_shutdown_runtime_error(e2):
                                    logger.warning(
                                        "[SUMMARY SCHEDULER SHUTDOWN GUARD] retry still failed label=%s error=%s -> return failed",
                                        label,
                                        e2,
                                    )
                                    return False, False, None
                                raise
                        return False, False, None

                _run_with_timeout_guarded._summary_shutdown_guard_v2 = True  # type: ignore[attr-defined]
                _run_with_timeout_guarded._summary_shutdown_guard_original = base_run_with_timeout  # type: ignore[attr-defined]
                scheduler._run_with_timeout = _run_with_timeout_guarded

            original_invoke_job = getattr(scheduler, "_invoke_job", None)
            if callable(original_invoke_job) and not getattr(original_invoke_job, "_summary_shutdown_guard_tick_v2", False):
                wrapped = _wrap_tick(original_invoke_job, name="_invoke_job")
                wrapped._summary_shutdown_guard_tick_v2 = True  # type: ignore[attr-defined]
                scheduler._invoke_job = wrapped

            for name in (
                "_run_push_summary_tick",
                "_run_ranking_summary_tick",
                "_run_summary_tick",
                "_run_push_fallback_when_unified_blocked",
                "_push_fallback_worker",
                "run_summary_tick_once",
            ):
                fn = getattr(scheduler, name, None)
                if callable(fn) and not getattr(fn, "_summary_shutdown_guard_tick_v2", False):
                    wrapped = _wrap_tick(fn, name=name)
                    wrapped._summary_shutdown_guard_tick_v2 = True  # type: ignore[attr-defined]
                    setattr(scheduler, name, wrapped)

            setattr(scheduler, "_SUMMARY_SCHEDULER_SHUTDOWN_GUARD_INSTALLED", True)
            setattr(scheduler, "_SUMMARY_SCHEDULER_SHUTDOWN_GUARD_V2_INSTALLED", True)
            _INSTALLED = True
            logger.warning("[SUMMARY SCHEDULER SHUTDOWN GUARD] installed V2 executor_recovery=True")
            return True

        except Exception:
            logger.exception("[SUMMARY SCHEDULER SHUTDOWN GUARD] install failed")
            return False
