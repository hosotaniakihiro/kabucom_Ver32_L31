# ============================================================
# File   : core/startup/main_schedule_due_filter_patch.py
# Version: V3-MAIN-SCHEDULE-DUE-FILTER-FULL-DEFAULT
# ------------------------------------------------------------
# main.py の schedule job を due/dispatch 直前で制御する。
#
# 2026-06-09 の NAS SQLite / WebSocket 不安定時には、job thread 起動直後に
# 0xC0000006 で落ちるケースがあったため entry_only 安全モードを追加した。
#
# V3:
#   デフォルトを full に戻す。
#   main.py で entry / exit_loop_5s / ranking / tonosama / summary AI を復帰する。
#   不安定時だけ AUTOSTOCK_MAIN_OPERATION_MODE=entry_only で安全モードへ戻せる。
#
# ENV staged restore / fallback:
#   AUTOSTOCK_MAIN_OPERATION_MODE=full|entry_exit|entry_only
#   AUTOSTOCK_MAIN_ENABLE_EXIT_LOOP=0/1
#   AUTOSTOCK_MAIN_ENABLE_RANKING_ENTRY=0/1
#   AUTOSTOCK_MAIN_ENABLE_TONOSAMA_ENTRY=0/1
#   AUTOSTOCK_MAIN_ENABLE_SUMMARY_AI_ENTRY=0/1
#   AUTOSTOCK_MAIN_ENABLE_SUMMARY_PARENT_TICK=0/1
#   AUTOSTOCK_MAIN_ENABLE_RANKING_SUMMARY_SCHEDULE=0/1
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


def _mode() -> str:
    try:
        return str(os.getenv("AUTOSTOCK_MAIN_OPERATION_MODE", "full") or "full").strip().lower()
    except Exception:
        return "full"


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


def _key_or_tags_contain(tags: set[str], key_s: str, *needles: str) -> bool:
    blob = " ".join([key_s, *sorted(tags)]).lower()
    return any(str(n).lower() in blob for n in needles)


def _allow_exit_loop() -> bool:
    mode = _mode()
    if mode in {"entry_exit", "full", "all"}:
        return _env_bool("AUTOSTOCK_MAIN_ENABLE_EXIT_LOOP", True)
    return _env_bool("AUTOSTOCK_MAIN_ENABLE_EXIT_LOOP", False)


def _allow_ranking_entry() -> bool:
    mode = _mode()
    if mode in {"full", "all"}:
        return _env_bool("AUTOSTOCK_MAIN_ENABLE_RANKING_ENTRY", True)
    return _env_bool("AUTOSTOCK_MAIN_ENABLE_RANKING_ENTRY", False)


def _allow_tonosama_entry() -> bool:
    mode = _mode()
    if mode in {"full", "all"}:
        return _env_bool("AUTOSTOCK_MAIN_ENABLE_TONOSAMA_ENTRY", True)
    return _env_bool("AUTOSTOCK_MAIN_ENABLE_TONOSAMA_ENTRY", False)


def _allow_summary_ai_entry() -> bool:
    mode = _mode()
    if mode in {"full", "all"}:
        return _env_bool("AUTOSTOCK_MAIN_ENABLE_SUMMARY_AI_ENTRY", True)
    return _env_bool("AUTOSTOCK_MAIN_ENABLE_SUMMARY_AI_ENTRY", False)


def _allow_ranking_summary() -> bool:
    mode = _mode()
    if mode in {"full", "all"}:
        return _env_bool("AUTOSTOCK_MAIN_ENABLE_RANKING_SUMMARY_SCHEDULE", True)
    return _env_bool("AUTOSTOCK_MAIN_ENABLE_RANKING_SUMMARY_SCHEDULE", False)


def _allow_summary_parent_tick() -> bool:
    mode = _mode()
    if mode in {"full", "all"}:
        return _env_bool("AUTOSTOCK_MAIN_ENABLE_SUMMARY_PARENT_TICK", True)
    return _env_bool("AUTOSTOCK_MAIN_ENABLE_SUMMARY_PARENT_TICK", False)


def _blocked_reason(job: Any, key: str | None = None) -> str | None:
    """main.py で thread 起動前に落とす schedule job を判定。"""
    if not _is_main_py():
        return None
    if not _env_bool("AUTOSTOCK_MAIN_SCHEDULE_DUE_FILTER", True):
        return None

    tags = _safe_tags_from_job(job)
    key_s = str(key or "")

    # Backward compatible hard-disable envs. Explicitly set only.
    if os.getenv("AUTOSTOCK_MAIN_DISABLE_SCHEDULED_ENTRY_JOBS") is not None:
        if _env_bool("AUTOSTOCK_MAIN_DISABLE_SCHEDULED_ENTRY_JOBS", False):
            if _key_or_tags_contain(tags, key_s, "tonosama_entry", "ranking_entry", "summary_ai"):
                return "main_py_entry_job_disabled_legacy_env"
    if os.getenv("AUTOSTOCK_MAIN_DISABLE_SCHEDULED_EXIT_LOOP") is not None:
        if _env_bool("AUTOSTOCK_MAIN_DISABLE_SCHEDULED_EXIT_LOOP", False) and _key_or_tags_contain(tags, key_s, "exit_loop_5s"):
            return "main_py_exit_loop_disabled_legacy_env"

    if _key_or_tags_contain(tags, key_s, "exit_loop_5s") and not _allow_exit_loop():
        return "main_py_exit_loop_disabled_stage"

    if _key_or_tags_contain(tags, key_s, "tonosama_entry") and not _allow_tonosama_entry():
        return "main_py_tonosama_entry_disabled_stage"

    if _key_or_tags_contain(tags, key_s, "ranking_entry") and not _allow_ranking_entry():
        return "main_py_ranking_entry_disabled_stage"

    if _key_or_tags_contain(tags, key_s, "summary_ai", "ai_summary", "summary_direct_dispatch") and not _allow_summary_ai_entry():
        return "main_py_summary_ai_disabled_stage"

    if _key_or_tags_contain(tags, key_s, "ranking_summary_all") and not _allow_ranking_summary():
        return "main_py_ranking_summary_disabled_stage"

    if _key_or_tags_contain(tags, key_s, "summary_parent_tick") and not _allow_summary_parent_tick():
        return "main_py_summary_parent_tick_disabled_stage"

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
                logger.warning(
                    "[MAIN SCHEDULE DUE FILTER] filtered due jobs before dispatch mode=%s skipped=%s kept=%s",
                    _mode(),
                    skipped,
                    [_safe_job_key(schedule_loop, j) for j in out],
                )
            return out

        def _patched_dispatch_due_job(job: Any, *args: Any, **kwargs: Any) -> bool:
            key = _safe_job_key(schedule_loop, job)
            reason = _blocked_reason(job, key)
            if reason:
                logger.warning(
                    "[MAIN SCHEDULE DUE FILTER] dispatch blocked before thread mode=%s key=%s tags=%s reason=%s",
                    _mode(),
                    key,
                    sorted(_safe_tags_from_job(job)),
                    reason,
                )
                _advance_job(schedule_loop, job)
                return False
            return bool(old_dispatch(job, *args, **kwargs))

        setattr(_patched_get_due_jobs, "_main_due_filter_wrapped", True)
        setattr(_patched_dispatch_due_job, "_main_due_filter_wrapped", True)
        schedule_loop._get_due_jobs = _patched_get_due_jobs
        schedule_loop._dispatch_due_job = _patched_dispatch_due_job

        _INSTALLED = True
        logger.warning(
            "[MAIN SCHEDULE DUE FILTER] installed v3 enabled=%s main_py=%s mode=%s allow_exit=%s allow_ranking=%s allow_tonosama=%s allow_summary_ai=%s allow_summary_parent=%s allow_ranking_summary=%s",
            _env_bool("AUTOSTOCK_MAIN_SCHEDULE_DUE_FILTER", True),
            _is_main_py(),
            _mode(),
            _allow_exit_loop(),
            _allow_ranking_entry(),
            _allow_tonosama_entry(),
            _allow_summary_ai_entry(),
            _allow_summary_parent_tick(),
            _allow_ranking_summary(),
        )
        return True
    except Exception:
        logger.exception("[MAIN SCHEDULE DUE FILTER] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[MAIN SCHEDULE DUE FILTER] auto install failed")
