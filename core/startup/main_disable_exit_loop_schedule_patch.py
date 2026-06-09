# ============================================================
# File   : core/startup/main_disable_exit_loop_schedule_patch.py
# Version: V1-MAIN-DISABLE-EXIT-LOOP-SCHEDULE
# ------------------------------------------------------------
# Purpose:
#   main.py 起動安定化用。
#
#   exit_loop_timeout_guard_patch 内の preflight skip は通常効くが、
#   schedule dispatch直後/preflightログ前に Windows 0xC0000006 で落ちるケースがある。
#   そのため main.py では exit_loop_5s の schedule job 自体を無効化する。
#
# ENV:
#   AUTOSTOCK_MAIN_DISABLE_SCHEDULED_EXIT_LOOP=1  # default in main.py
# ============================================================
from __future__ import annotations

import logging
import os
import sys

logger = logging.getLogger(__name__)
_INSTALLED = False


def _env_bool(name: str, default: bool) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _is_main_py_process() -> bool:
    try:
        return os.path.basename(str(sys.argv[0] or "")).lower() == "main.py"
    except Exception:
        return False


def _disabled() -> bool:
    if not _is_main_py_process():
        return False
    return _env_bool("AUTOSTOCK_MAIN_DISABLE_SCHEDULED_EXIT_LOOP", True)


def _noop_exit_loop(*args, **kwargs):
    logger.info(
        "[MAIN DISABLE EXIT LOOP SCHEDULE] skipped exit_loop_5s schedule job in main.py. "
        "Set AUTOSTOCK_MAIN_DISABLE_SCHEDULED_EXIT_LOOP=0 to restore."
    )
    return None


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    if not _disabled():
        logger.warning(
            "[MAIN DISABLE EXIT LOOP SCHEDULE] install skipped enabled=%s main_py=%s",
            _env_bool("AUTOSTOCK_MAIN_DISABLE_SCHEDULED_EXIT_LOOP", True),
            _is_main_py_process(),
        )
        _INSTALLED = True
        return False

    changed = 0
    try:
        import core.startup.scheduler_exit_bootstrap as boot
        boot.run_exit_loop_market_guarded = _noop_exit_loop
        changed += 1
    except Exception:
        logger.debug("[MAIN DISABLE EXIT LOOP SCHEDULE] scheduler_exit_bootstrap patch skipped", exc_info=True)

    try:
        import core.startup.exit_loop_timeout_guard_patch as timeout_guard
        timeout_guard._patched_run_exit_loop_market_guarded = _noop_exit_loop  # type: ignore[attr-defined]
        changed += 1
    except Exception:
        logger.debug("[MAIN DISABLE EXIT LOOP SCHEDULE] timeout_guard module patch skipped", exc_info=True)

    removed = 0
    replaced = 0
    try:
        import schedule
        jobs = list(getattr(schedule, "jobs", []) or [])
        for job in jobs:
            try:
                tags = set(getattr(job, "tags", set()) or set())
                if "exit_loop_5s" in tags or "exit" in tags:
                    # schedule.cancel_job が使える場合は jobごと消す。
                    try:
                        schedule.cancel_job(job)
                        removed += 1
                    except Exception:
                        job.job_func = _noop_exit_loop
                        replaced += 1
            except Exception:
                pass
    except Exception:
        logger.debug("[MAIN DISABLE EXIT LOOP SCHEDULE] schedule scan skipped", exc_info=True)

    os.environ.setdefault("AUTOSTOCK_MAIN_DISABLE_SCHEDULED_EXIT_LOOP", "1")
    _INSTALLED = True
    logger.warning(
        "[MAIN DISABLE EXIT LOOP SCHEDULE] installed enabled=True main_py=True changed=%s removed_jobs=%s replaced_jobs=%s",
        changed,
        removed,
        replaced,
    )
    return True


try:
    install()
except Exception:
    logger.exception("[MAIN DISABLE EXIT LOOP SCHEDULE] auto install failed")


__all__ = ["install"]
