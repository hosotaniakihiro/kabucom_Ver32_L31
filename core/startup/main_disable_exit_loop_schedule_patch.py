# ============================================================
# File   : core/startup/main_disable_exit_loop_schedule_patch.py
# Version: V3-MAIN-STAGED-ENTRY-EXIT-SCHEDULE
# ------------------------------------------------------------
# Purpose:
#   main.py 起動安定化用。
#
#   関数内の skip/preflight は通常効くが、schedule dispatch直後や
#   job thread start直後、skipログ前に Windows 0xC0000006 で落ちるケースがある。
#   そのため main.py では DB/cache参照へ進み得る schedule job を
#   段階復帰スイッチで制御する。
#
# V3 staged restore:
#   default mode = entry_only
#
#   entry_only:
#     - entry/order の軽量基盤は残す
#     - exit_loop_5s は止める
#     - ranking_entry / tonosama_entry / summary_ai は止める
#
#   entry_exit:
#     - exit_loop_5s を戻す
#     - ranking/tonosama/summary_ai はまだ止める
#
#   full:
#     - ranking/tonosama/summary_ai も戻す
#
# ENV:
#   AUTOSTOCK_MAIN_OPERATION_MODE=entry_only|entry_exit|full
#   AUTOSTOCK_MAIN_ENABLE_EXIT_LOOP=1
#   AUTOSTOCK_MAIN_ENABLE_RANKING_ENTRY=1
#   AUTOSTOCK_MAIN_ENABLE_TONOSAMA_ENTRY=1
#   AUTOSTOCK_MAIN_ENABLE_SUMMARY_AI_ENTRY=1
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
        return str(os.getenv("AUTOSTOCK_MAIN_OPERATION_MODE", "entry_only") or "entry_only").strip().lower()
    except Exception:
        return "entry_only"


def _is_main_py_process() -> bool:
    try:
        return os.path.basename(str(sys.argv[0] or "")).lower() == "main.py"
    except Exception:
        return False


def _allow_exit() -> bool:
    if not _is_main_py_process():
        return True
    if _mode() in {"entry_exit", "full", "all"}:
        return True
    return _env_bool("AUTOSTOCK_MAIN_ENABLE_EXIT_LOOP", False)


def _allow_ranking_entry() -> bool:
    if not _is_main_py_process():
        return True
    if _mode() in {"full", "all"}:
        return True
    return _env_bool("AUTOSTOCK_MAIN_ENABLE_RANKING_ENTRY", False)


def _allow_tonosama_entry() -> bool:
    if not _is_main_py_process():
        return True
    if _mode() in {"full", "all"}:
        return True
    return _env_bool("AUTOSTOCK_MAIN_ENABLE_TONOSAMA_ENTRY", False)


def _disable_exit() -> bool:
    if not _is_main_py_process():
        return False
    # legacy env can still hard-disable if explicitly set.
    if os.getenv("AUTOSTOCK_MAIN_DISABLE_SCHEDULED_EXIT_LOOP") is not None:
        return _env_bool("AUTOSTOCK_MAIN_DISABLE_SCHEDULED_EXIT_LOOP", False)
    return not _allow_exit()


def _disable_ranking_entry() -> bool:
    if not _is_main_py_process():
        return False
    # legacy blanket disable only if explicitly set.
    if os.getenv("AUTOSTOCK_MAIN_DISABLE_SCHEDULED_ENTRY_JOBS") is not None and _env_bool("AUTOSTOCK_MAIN_DISABLE_SCHEDULED_ENTRY_JOBS", False):
        return True
    return not _allow_ranking_entry()


def _disable_tonosama_entry() -> bool:
    if not _is_main_py_process():
        return False
    # legacy blanket disable only if explicitly set.
    if os.getenv("AUTOSTOCK_MAIN_DISABLE_SCHEDULED_ENTRY_JOBS") is not None and _env_bool("AUTOSTOCK_MAIN_DISABLE_SCHEDULED_ENTRY_JOBS", False):
        return True
    return not _allow_tonosama_entry()


def _noop_exit_loop(*args, **kwargs):
    logger.info(
        "[MAIN STAGED ENTRY/EXIT SCHEDULE] skipped exit_loop_5s in main.py mode=%s. "
        "Set AUTOSTOCK_MAIN_OPERATION_MODE=entry_exit or AUTOSTOCK_MAIN_ENABLE_EXIT_LOOP=1 to restore.",
        _mode(),
    )
    return None


def _noop_tonosama_entry(*args, **kwargs):
    logger.info(
        "[MAIN STAGED ENTRY/EXIT SCHEDULE] skipped tonosama_entry in main.py mode=%s. "
        "Set AUTOSTOCK_MAIN_ENABLE_TONOSAMA_ENTRY=1 or AUTOSTOCK_MAIN_OPERATION_MODE=full to restore.",
        _mode(),
    )
    return 0


def _noop_ranking_entry(*args, **kwargs):
    logger.info(
        "[MAIN STAGED ENTRY/EXIT SCHEDULE] skipped ranking_entry in main.py mode=%s. "
        "Set AUTOSTOCK_MAIN_ENABLE_RANKING_ENTRY=1 or AUTOSTOCK_MAIN_OPERATION_MODE=full to restore.",
        _mode(),
    )
    return 0


def _should_disable_job(tags: set[str], func_name: str) -> tuple[bool, str]:
    tags_s = {str(t) for t in tags}
    blob = " ".join([str(func_name or ""), *sorted(tags_s)]).lower()

    if _disable_exit() and ("exit_loop_5s" in tags_s or "exit_loop_5s" in blob or "exit_loop" in blob):
        return True, "exit_loop_5s"

    if _disable_tonosama_entry() and ("tonosama_entry" in tags_s or "tonosama_entry" in blob or "tonosama" in blob):
        return True, "tonosama_entry"

    if _disable_ranking_entry() and ("ranking_entry" in tags_s or "ranking_entry" in blob or "_run_ranking_entry" in blob):
        return True, "ranking_entry"

    return False, ""


def _patch_direct_refs() -> int:
    changed = 0

    if _disable_exit():
        try:
            import core.startup.scheduler_exit_bootstrap as boot
            boot.run_exit_loop_market_guarded = _noop_exit_loop
            changed += 1
        except Exception:
            logger.debug("[MAIN STAGED ENTRY/EXIT SCHEDULE] scheduler_exit_bootstrap patch skipped", exc_info=True)

        try:
            import core.startup.exit_loop_timeout_guard_patch as timeout_guard
            timeout_guard._patched_run_exit_loop_market_guarded = _noop_exit_loop  # type: ignore[attr-defined]
            changed += 1
        except Exception:
            logger.debug("[MAIN STAGED ENTRY/EXIT SCHEDULE] timeout_guard module patch skipped", exc_info=True)

    try:
        import trading.entry_exit.tasks as tasks
        if _disable_tonosama_entry() and hasattr(tasks, "_run_tonosama_entry_safe"):
            tasks._run_tonosama_entry_safe = _noop_tonosama_entry
            changed += 1
        if _disable_ranking_entry() and hasattr(tasks, "_run_ranking_entry_safe"):
            tasks._run_ranking_entry_safe = _noop_ranking_entry
            changed += 1
    except Exception:
        logger.debug("[MAIN STAGED ENTRY/EXIT SCHEDULE] entry_exit.tasks patch skipped", exc_info=True)

    if _disable_tonosama_entry():
        try:
            import core.startup.tonosama_skip_build_when_pending_exists_patch as tono
            if hasattr(tono, "patched"):
                tono.patched = _noop_tonosama_entry
                changed += 1
        except Exception:
            logger.debug("[MAIN STAGED ENTRY/EXIT SCHEDULE] tonosama patch ref skipped", exc_info=True)

    if _disable_ranking_entry():
        try:
            import core.startup.ranking_stuck_pending_prune_patch as rank
            if hasattr(rank, "patched"):
                rank.patched = _noop_ranking_entry
                changed += 1
        except Exception:
            logger.debug("[MAIN STAGED ENTRY/EXIT SCHEDULE] ranking patch ref skipped", exc_info=True)

    return changed


def _scan_schedule_jobs() -> tuple[int, int, int]:
    scanned = 0
    removed = 0
    replaced = 0
    try:
        import schedule
        jobs = list(getattr(schedule, "jobs", []) or [])
        for job in jobs:
            scanned += 1
            try:
                tags = set(getattr(job, "tags", set()) or set())
                fn_obj = getattr(job, "job_func", None)
                try:
                    fn_name = getattr(fn_obj, "__name__", "") or repr(fn_obj)
                except Exception:
                    fn_name = repr(fn_obj)
                disable, reason = _should_disable_job(tags, fn_name)
                if not disable:
                    continue

                noop = _noop_exit_loop if reason == "exit_loop_5s" else (_noop_tonosama_entry if reason == "tonosama_entry" else _noop_ranking_entry)

                try:
                    job.job_func = noop
                    replaced += 1
                except Exception:
                    pass

                try:
                    schedule.cancel_job(job)
                    removed += 1
                except Exception:
                    pass

                logger.warning(
                    "[MAIN STAGED ENTRY/EXIT SCHEDULE] disabled scheduled job mode=%s reason=%s tags=%s func=%s",
                    _mode(),
                    reason,
                    sorted(str(t) for t in tags),
                    fn_name,
                )
            except Exception:
                logger.debug("[MAIN STAGED ENTRY/EXIT SCHEDULE] per-job scan skipped", exc_info=True)
    except Exception:
        logger.debug("[MAIN STAGED ENTRY/EXIT SCHEDULE] schedule scan skipped", exc_info=True)
    return scanned, removed, replaced


def install() -> bool:
    global _INSTALLED
    # 複数回呼ばれても、後から追加されたschedule jobを消すため scan は毎回行う。
    if not (_disable_exit() or _disable_ranking_entry() or _disable_tonosama_entry()):
        logger.warning(
            "[MAIN STAGED ENTRY/EXIT SCHEDULE] install noop main_py=%s mode=%s allow_exit=%s allow_ranking=%s allow_tonosama=%s",
            _is_main_py_process(),
            _mode(),
            _allow_exit(),
            _allow_ranking_entry(),
            _allow_tonosama_entry(),
        )
        _INSTALLED = True
        return False

    changed = _patch_direct_refs()
    scanned, removed, replaced = _scan_schedule_jobs()

    _INSTALLED = True
    logger.warning(
        "[MAIN STAGED ENTRY/EXIT SCHEDULE] installed v3 main_py=%s mode=%s disable_exit=%s disable_ranking=%s disable_tonosama=%s changed=%s scanned_jobs=%s removed_jobs=%s replaced_jobs=%s",
        _is_main_py_process(),
        _mode(),
        _disable_exit(),
        _disable_ranking_entry(),
        _disable_tonosama_entry(),
        changed,
        scanned,
        removed,
        replaced,
    )
    return True


try:
    install()
except Exception:
    logger.exception("[MAIN STAGED ENTRY/EXIT SCHEDULE] auto install failed")


__all__ = ["install"]
