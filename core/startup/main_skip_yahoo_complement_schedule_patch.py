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
_ORIG_YAHOO_JOB = None


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


def _full_yahoo_enabled() -> bool:
    return _env_on("AUTOSTOCK_ENABLE_YAHOO_COMPLEMENT_IN_MAIN", False) or _env_on("YAHOO_COMPLEMENT_RUN_IN_MAIN", False)


def _memory_enabled() -> bool:
    # main.py should use Yahoo補完 immediately, but only as a cache-only path.
    # DB writes remain disabled in main.py and are handled by main_database.py.
    return _is_main_py() and not _full_yahoo_enabled() and _env_on("AUTOSTOCK_MAIN_YAHOO_MEMORY_COMPLEMENT", True)


def _skip_enabled() -> bool:
    # AUTOSTOCK_MAIN_SKIP_YAHOO_COMPLEMENT=1 means: skip the heavy DB-writing Yahoo job.
    # It must not disable the memory-only path requested for main.py immediate use.
    return (
        _is_main_py()
        and _env_on("AUTOSTOCK_MAIN_SKIP_YAHOO_COMPLEMENT", True)
        and not _full_yahoo_enabled()
        and not _memory_enabled()
    )


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
        logger.exception("[MAIN YAHOO COMPLEMENT POLICY] remove yahoo jobs failed")
    return removed


def _run_memory_yahoo_job():
    from trading.yahoo.complement.download_flow import run_periodic_yahoo_complement_main_cache_only

    logger.warning("[MAIN YAHOO COMPLEMENT POLICY] memory-only yahoo job start save_db=0 update_cache=1")
    return run_periodic_yahoo_complement_main_cache_only()


def _patch_yahoo_module() -> bool:
    """Apply main.py policy: DB-writing Yahoo is blocked; memory-only Yahoo is allowed by default."""
    global _ORIG_REGISTER_YAHOO, _ORIG_YAHOO_WRAPPER, _ORIG_YAHOO_JOB
    try:
        import core.yahoo_tasks as yt
    except Exception:
        return False

    changed = False

    try:
        cur_job = getattr(yt, "yahoo_minutely_complement_job", None)
        if callable(cur_job) and not getattr(cur_job, "_main_yahoo_policy_v5", False):
            _ORIG_YAHOO_JOB = cur_job

            def yahoo_job_patched(*args: Any, **kwargs: Any):
                if _memory_enabled():
                    return _run_memory_yahoo_job()
                if _skip_enabled():
                    logger.warning("[MAIN YAHOO COMPLEMENT POLICY] yahoo job fully skipped in main.py")
                    return None
                return _ORIG_YAHOO_JOB(*args, **kwargs)

            yahoo_job_patched._main_yahoo_policy_v5 = True
            yt.yahoo_minutely_complement_job = yahoo_job_patched
            changed = True
    except Exception:
        logger.exception("[MAIN YAHOO COMPLEMENT POLICY] patch yahoo_minutely_complement_job failed")

    try:
        cur_register = getattr(yt, "register_yahoo_tasks", None)
        if callable(cur_register) and not getattr(cur_register, "_main_yahoo_policy_v5", False):
            _ORIG_REGISTER_YAHOO = cur_register

            def register_patched(*args: Any, **kwargs: Any):
                if _memory_enabled():
                    logger.warning(
                        "[MAIN YAHOO COMPLEMENT POLICY] register allowed as memory-only in main.py argv=%s",
                        sys.argv,
                    )
                    return _ORIG_REGISTER_YAHOO(*args, **kwargs)
                if _skip_enabled():
                    removed = _remove_yahoo_jobs()
                    logger.warning(
                        "[MAIN YAHOO COMPLEMENT POLICY] register fully blocked in main.py removed=%s argv=%s",
                        removed,
                        sys.argv,
                    )
                    return False
                return _ORIG_REGISTER_YAHOO(*args, **kwargs)

            register_patched._main_yahoo_policy_v5 = True
            yt.register_yahoo_tasks = register_patched
            changed = True
    except Exception:
        logger.exception("[MAIN YAHOO COMPLEMENT POLICY] patch register_yahoo_tasks failed")

    try:
        cur_wrapper = getattr(yt, "_yahoo_wrapper", None)
        if callable(cur_wrapper) and not getattr(cur_wrapper, "_main_yahoo_policy_v5", False):
            _ORIG_YAHOO_WRAPPER = cur_wrapper

            def yahoo_wrapper_patched(*args: Any, **kwargs: Any):
                if _memory_enabled():
                    return _run_memory_yahoo_job()
                if _skip_enabled():
                    logger.warning("[MAIN YAHOO COMPLEMENT POLICY] _yahoo_wrapper fully blocked in main.py")
                    return None
                return _ORIG_YAHOO_WRAPPER(*args, **kwargs)

            yahoo_wrapper_patched._main_yahoo_policy_v5 = True
            yt._yahoo_wrapper = yahoo_wrapper_patched
            changed = True
    except Exception:
        logger.exception("[MAIN YAHOO COMPLEMENT POLICY] patch _yahoo_wrapper failed")

    return changed


def install() -> bool:
    global _INSTALLED, _ORIG_DISPATCH

    if not _is_main_py():
        logger.warning("[MAIN YAHOO COMPLEMENT POLICY] skipped non-main context argv=%s", sys.argv)
        return False

    try:
        # main.py default: use Yahoo immediately as memory/cache only; never do heavy DB writes.
        os.environ.setdefault("AUTOSTOCK_MAIN_SKIP_YAHOO_COMPLEMENT", "1")
        os.environ.setdefault("AUTOSTOCK_MAIN_YAHOO_MEMORY_COMPLEMENT", "1")
        os.environ.setdefault("YAHOO_COMPLEMENT_RUN_IN_MAIN", "0")
        os.environ.setdefault("AUTOSTOCK_ENABLE_YAHOO_COMPLEMENT_IN_MAIN", "0")
        os.environ.setdefault("AUTOSTOCK_MAIN_YAHOO_MEMORY_MAX_SYMBOLS", "40")
        os.environ.setdefault("AUTOSTOCK_MAIN_YAHOO_MEMORY_BUDGET_SEC", "35")

        import schedule
        from core.startup import schedule_loop as sl
    except Exception:
        logger.exception("[MAIN YAHOO COMPLEMENT POLICY] import failed")
        return False

    try:
        module_patched = _patch_yahoo_module()

        if _ORIG_DISPATCH is None and hasattr(sl, "_dispatch_due_job"):
            _ORIG_DISPATCH = sl._dispatch_due_job

            def _dispatch_patched(job: Any, *args: Any, **kwargs: Any) -> bool:
                if _skip_enabled() and _is_yahoo_complement_job(job):
                    _advance_next_run(job)
                    logger.warning(
                        "[MAIN YAHOO COMPLEMENT POLICY] skipped due job tags=%s func=%s next_run=%s",
                        _safe_tags(job),
                        _job_func_name(job),
                        getattr(job, "next_run", None),
                    )
                    return False
                return _ORIG_DISPATCH(job, *args, **kwargs)

            sl._dispatch_due_job = _dispatch_patched

        removed = _remove_yahoo_jobs() if _skip_enabled() else 0
        _INSTALLED = True
        logger.warning(
            "[MAIN YAHOO COMPLEMENT POLICY] installed v5 mode=%s removed=%s module_patched=%s jobs=%s memory=%s skip=%s argv=%s",
            "full" if _full_yahoo_enabled() else ("memory" if _memory_enabled() else "skip"),
            removed,
            module_patched,
            len(getattr(schedule, "jobs", []) or []),
            os.getenv("AUTOSTOCK_MAIN_YAHOO_MEMORY_COMPLEMENT"),
            os.getenv("AUTOSTOCK_MAIN_SKIP_YAHOO_COMPLEMENT"),
            sys.argv,
        )
        return True
    except Exception:
        logger.exception("[MAIN YAHOO COMPLEMENT POLICY] install failed")
        return False
