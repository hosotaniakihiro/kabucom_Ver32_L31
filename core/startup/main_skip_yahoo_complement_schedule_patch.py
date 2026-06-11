from __future__ import annotations

import datetime as dt
import logging
import os
import sys
from typing import Any

logger = logging.getLogger(__name__)

_INSTALLED = False
_ORIG_DISPATCH = None
_ORIG_DISPATCH_ONCE = None


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


def install() -> bool:
    global _INSTALLED, _ORIG_DISPATCH, _ORIG_DISPATCH_ONCE

    if _INSTALLED:
        return True
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
        if _ORIG_DISPATCH is None and hasattr(sl, "_dispatch_due_job"):
            _ORIG_DISPATCH = sl._dispatch_due_job

            def _dispatch_patched(job: Any, *args: Any, **kwargs: Any) -> bool:
                if _is_yahoo_complement_job(job):
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

        # Proactively move any already-due yahoo complement jobs away at install time.
        skipped = 0
        for job in list(getattr(schedule, "jobs", []) or []):
            if _is_yahoo_complement_job(job):
                _advance_next_run(job)
                skipped += 1

        _INSTALLED = True
        logger.warning(
            "[MAIN SKIP YAHOO COMPLEMENT] installed v1 enabled=True pre_advanced=%s argv=%s",
            skipped,
            sys.argv,
        )
        return True
    except Exception:
        logger.exception("[MAIN SKIP YAHOO COMPLEMENT] install failed")
        return False
