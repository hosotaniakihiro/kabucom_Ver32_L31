from __future__ import annotations

import datetime as dt
import logging
import os
import sys
from typing import Any

logger = logging.getLogger(__name__)

_INSTALLED = False
_ORIG_DISPATCH = None
_ORIG_REGISTER_YAHOO = None
_ORIG_YAHOO_WRAPPER = None


def _is_main_py() -> bool:
    try:
        argv = " ".join(str(x).replace("\\", "/").lower() for x in sys.argv)
        if "main_database.py" in argv or "yahoo_complement_runner.py" in argv or "summary_database_runner.py" in argv:
            return False
        return "main.py" in argv
    except Exception:
        return False


def _env_on(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _skip_enabled() -> bool:
    return _is_main_py() and _env_on("AUTOSTOCK_MAIN_SKIP_YAHOO_COMPLEMENT", True) and not _env_on("AUTOSTOCK_ENABLE_YAHOO_COMPLEMENT_IN_MAIN", False) and not _env_on("YAHOO_COMPLEMENT_RUN_IN_MAIN", False)


def _safe_tags(job: Any) -> list[str]:
    try:
        return [str(x) for x in (getattr(job, "tags", set()) or set())]
    except Exception:
        return []


def _job_func_name(job: Any) -> str:
    try:
        fn = getattr(job, "job_func", None)
        real_fn = getattr(fn, "func", fn)
        mod = getattr(real_fn, "__module__", "") or ""
        name = getattr(real_fn, "__name__", "") or ""
        return f"{mod}.{name}" if mod or name else repr(fn)
    except Exception:
        return ""


def _is_yahoo_complement_job(job: Any) -> bool:
    try:
        tags = set(_safe_tags(job))
        if "yahoo_complement_database_owner" in tags:
            return True
        func = _job_func_name(job).lower()
        text = (repr(job) + " " + func).lower()
        return "yahoo" in text and ("complement" in text or "_yahoo_wrapper" in text)
    except Exception:
        return False


def _advance_next_run(job: Any) -> None:
    try:
        fn = getattr(job, "_schedule_next_run", None)
        if callable(fn):
            fn()
            return
    except Exception:
        pass
    try:
        setattr(job, "last_run", dt.datetime.now())
        setattr(job, "next_run", dt.datetime.now() + dt.timedelta(minutes=1))
    except Exception:
        pass


def _remove_yahoo_jobs() -> int:
    try:
        import schedule
    except Exception:
        return 0
    removed = 0
    try:
        for job in list(getattr(schedule, "jobs", []) or []):
            if _is_yahoo_complement_job(job):
                try:
                    schedule.cancel_job(job)
                    removed += 1
                except Exception:
                    _advance_next_run(job)
                    removed += 1
    except Exception:
        logger.exception("[MAIN SKIP YAHOO COMPLEMENT] remove yahoo jobs failed")
    return removed


def _patch_yahoo_module() -> bool:
    """Hard block Yahoo complement inside main.py, even if registration happens after this patch."""
    global _ORIG_REGISTER_YAHOO, _ORIG_YAHOO_WRAPPER
    try:
        import core.yahoo_tasks as yt
    except Exception:
        # yahoo_tasks may not be imported yet. schedule-loop dispatch patch still protects due jobs.
        return False

    changed = False
    try:
        cur_register = getattr(yt, "register_yahoo_tasks", None)
        if callable(cur_register) and not getattr(cur_register, "_main_skip_yahoo_v2", False):
            _ORIG_REGISTER_YAHOO = cur_register

            def register_patched(*args: Any, **kwargs: Any):
                if _skip_enabled():
                    removed = _remove_yahoo_jobs()
                    logger.warning(
                        "[MAIN SKIP YAHOO COMPLEMENT] register blocked in main.py removed=%s argv=%s",
                        removed,
                        sys.argv,
                    )
                    return False
                return _ORIG_REGISTER_YAHOO(*args, **kwargs)

            register_patched._main_skip_yahoo_v2 = True
            yt.register_yahoo_tasks = register_patched
            changed = True
    except Exception:
        logger.exception("[MAIN SKIP YAHOO COMPLEMENT] patch register_yahoo_tasks failed")

    try:
        cur_wrapper = getattr(yt, "_yahoo_wrapper", None)
        if callable(cur_wrapper) and not getattr(cur_wrapper, "_main_skip_yahoo_v2", False):
            _ORIG_YAHOO_WRAPPER = cur_wrapper

            def yahoo_wrapper_patched(*args: Any, **kwargs: Any):
                if _skip_enabled():
                    logger.warning("[MAIN SKIP YAHOO COMPLEMENT] _yahoo_wrapper blocked in main.py")
                    return None
                return _ORIG_YAHOO_WRAPPER(*args, **kwargs)

            yahoo_wrapper_patched._main_skip_yahoo_v2 = True
            yt._yahoo_wrapper = yahoo_wrapper_patched
            changed = True
    except Exception:
        logger.exception("[MAIN SKIP YAHOO COMPLEMENT] patch _yahoo_wrapper failed")

    return changed


def install() -> bool:
    global _INSTALLED, _ORIG_DISPATCH

    if not _is_main_py():
        logger.warning("[MAIN SKIP YAHOO COMPLEMENT] skipped non-main context argv=%s", sys.argv)
        return False
    if not _env_on("AUTOSTOCK_MAIN_SKIP_YAHOO_COMPLEMENT", True):
        logger.warning("[MAIN SKIP YAHOO COMPLEMENT] disabled by env")
        return False

    try:
        import schedule
        from core.startup import schedule_loop as sl
    except Exception:
        logger.exception("[MAIN SKIP YAHOO COMPLEMENT] import failed")
        return False

    try:
        module_patched = _patch_yahoo_module()

        if _ORIG_DISPATCH is None and hasattr(sl, "_dispatch_due_job"):
            _ORIG_DISPATCH = sl._dispatch_due_job

            def _dispatch_patched(job: Any, *args: Any, **kwargs: Any) -> bool:
                if _skip_enabled() and _is_yahoo_complement_job(job):
                    _advance_next_run(job)
                    logger.warning(
                        "[MAIN SKIP YAHOO COMPLEMENT] skipped due job tags=%s func=%s next_run=%s",
                        _safe_tags(job),
                        _job_func_name(job),
                        getattr(job, "next_run", None),
                    )
                    return False
                return _ORIG_DISPATCH(job, *args, **kwargs)

            sl._dispatch_due_job = _dispatch_patched

        removed = _remove_yahoo_jobs()
        _INSTALLED = True
        logger.warning(
            "[MAIN SKIP YAHOO COMPLEMENT] installed v2 enabled=True removed=%s module_patched=%s jobs=%s argv=%s",
            removed,
            module_patched,
            len(getattr(schedule, "jobs", []) or []),
            sys.argv,
        )
        return True
    except Exception:
        logger.exception("[MAIN SKIP YAHOO COMPLEMENT] install failed")
        return False
