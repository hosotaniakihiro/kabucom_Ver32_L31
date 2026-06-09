# ============================================================
# File   : core/startup/main_schedule_due_filter_patch.py
# Version: V1-MAIN-SCHEDULE-DUE-FILTER
# ------------------------------------------------------------
# main.py は entry/order 側の軽量プロセスとして起動させる。
# NAS SQLite が不安定な環境では、関数内 skip まで到達する前に
# schedule job thread 起動直後で 0xC0000006 になるケースがある。
#
# そのため schedule_loop の due job 検出/dispatch の最外層で、
# main.py では DB owner 系 job を thread 起動前に除外する。
# ============================================================
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)
_INSTALLED = False


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        s = str(v).strip().lower()
        if s in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}:
            return True
        if s in {"0", "false", "no", "n", "off", "ng", "disable", "disabled"}:
            return False
        return bool(default)
    except Exception:
        return bool(default)


def _is_main_py() -> bool:
    try:
        return Path(str(sys.argv[0] or "")).name.lower() == "main.py"
    except Exception:
        return False


def _safe_tags_from_job(job: Any) -> set[str]:
    try:
        return {str(x) for x in (getattr(job, "tags", set()) or set())}
    except Exception:
        return set()


def _safe_job_key(schedule_loop: Any, job: Any) -> str:
    try:
        fn = getattr(schedule_loop, "_job_key", None)
        if callable(fn):
            return str(fn(job))
    except Exception:
        pass
    try:
        tags = sorted(_safe_tags_from_job(job))
        if tags:
            return "tags:" + ",".join(tags)
    except Exception:
        pass
    return repr(job)


def _blocked_reason(job: Any, key: str | None = None) -> str | None:
    """main.py で thread 起動前に落とす schedule job を判定。"""
    if not _is_main_py():
        return None
    if not _env_bool("AUTOSTOCK_MAIN_SCHEDULE_DUE_FILTER", True):
        return None

    tags = _safe_tags_from_job(job)
    key_s = str(key or "")

    disable_entry = _env_bool("AUTOSTOCK_MAIN_DISABLE_SCHEDULED_ENTRY_JOBS", True)
    disable_exit = _env_bool("AUTOSTOCK_MAIN_DISABLE_SCHEDULED_EXIT_LOOP", True)
    disable_ranking_summary = _env_bool("AUTOSTOCK_MAIN_SKIP_RANKING_SUMMARY_SCHEDULE", True)
    disable_summary_parent = _env_bool("AUTOSTOCK_MAIN_SKIP_SUMMARY_PARENT_TICK", True)

    if disable_entry and "entry" in tags and ({"tonosama_entry", "ranking_entry"} & tags):
        return "main_py_entry_job_disabled"
    if disable_exit and "exit_loop_5s" in tags:
        return "main_py_exit_loop_disabled"
    if disable_ranking_summary and "ranking_summary_all" in tags:
        return "main_py_ranking_summary_disabled"
    if disable_summary_parent and "summary_parent_tick" in tags:
        return "main_py_summary_parent_tick_disabled"

    # 念のため key 文字列でも判定する。
    if disable_entry and ("tonosama_entry" in key_s or "ranking_entry" in key_s):
        return "main_py_entry_job_disabled_key"
    if disable_exit and "exit_loop_5s" in key_s:
        return "main_py_exit_loop_disabled_key"
    if disable_ranking_summary and "ranking_summary_all" in key_s:
        return "main_py_ranking_summary_disabled_key"
    if disable_summary_parent and "summary_parent_tick" in key_s:
        return "main_py_summary_parent_tick_disabled_key"

    return None


def _advance_job(schedule_loop: Any, job: Any) -> None:
    try:
        fn = getattr(schedule_loop, "_safe_schedule_next_run", None)
        if callable(fn):
            fn(job)
            return
    except Exception:
        logger.debug("[MAIN SCHEDULE DUE FILTER] _safe_schedule_next_run failed", exc_info=True)
    try:
        raw = getattr(job, "_schedule_next_run", None)
        if callable(raw):
            raw()
    except Exception:
        logger.debug("[MAIN SCHEDULE DUE FILTER] job._schedule_next_run failed", exc_info=True)


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    try:
        if not _env_bool("AUTOSTOCK_MAIN_SCHEDULE_DUE_FILTER", True):
            logger.warning("[MAIN SCHEDULE DUE FILTER] skipped by env")
            return False

        from core.startup import schedule_loop  # type: ignore

        old_get_due = getattr(schedule_loop, "_get_due_jobs", None)
        old_dispatch = getattr(schedule_loop, "_dispatch_due_job", None)
        if not callable(old_get_due) or not callable(old_dispatch):
            logger.warning("[MAIN SCHEDULE DUE FILTER] target functions missing")
            return False
        if getattr(old_get_due, "_main_due_filter_wrapped", False) or getattr(old_dispatch, "_main_due_filter_wrapped", False):
            _INSTALLED = True
            return True

        def _patched_get_due_jobs() -> list[Any]:
            due = list(old_get_due() or [])
            if not _is_main_py():
                return due
            out: list[Any] = []
            skipped: list[str] = []
            for job in due:
                key = _safe_job_key(schedule_loop, job)
                reason = _blocked_reason(job, key)
                if reason:
                    skipped.append(f"{key}:{reason}")
                    _advance_job(schedule_loop, job)
                    continue
                out.append(job)
            if skipped:
                logger.warning("[MAIN SCHEDULE DUE FILTER] filtered due jobs before dispatch skipped=%s kept=%s", skipped, [_safe_job_key(schedule_loop, j) for j in out])
            return out

        def _patched_dispatch_due_job(job: Any, *args: Any, **kwargs: Any) -> bool:
            key = _safe_job_key(schedule_loop, job)
            reason = _blocked_reason(job, key)
            if reason:
                logger.warning("[MAIN SCHEDULE DUE FILTER] dispatch blocked before thread key=%s tags=%s reason=%s", key, sorted(_safe_tags_from_job(job)), reason)
                _advance_job(schedule_loop, job)
                return False
            return bool(old_dispatch(job, *args, **kwargs))

        setattr(_patched_get_due_jobs, "_main_due_filter_wrapped", True)
        setattr(_patched_dispatch_due_job, "_main_due_filter_wrapped", True)
        schedule_loop._get_due_jobs = _patched_get_due_jobs
        schedule_loop._dispatch_due_job = _patched_dispatch_due_job

        _INSTALLED = True
        logger.warning(
            "[MAIN SCHEDULE DUE FILTER] installed v1 enabled=%s main_py=%s",
            _env_bool("AUTOSTOCK_MAIN_SCHEDULE_DUE_FILTER", True),
            _is_main_py(),
        )
        return True
    except Exception:
        logger.exception("[MAIN SCHEDULE DUE FILTER] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[MAIN SCHEDULE DUE FILTER] auto install failed")
