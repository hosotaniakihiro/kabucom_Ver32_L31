# ============================================================
# File   : core/startup/main_disable_exit_loop_schedule_patch.py
# Version: V2-MAIN-DISABLE-ENTRY-EXIT-SCHEDULE
# ------------------------------------------------------------
# Purpose:
#   main.py 起動安定化用。
#
#   関数内の skip/preflight は通常効くが、schedule dispatch直後や
#   job thread start直後、skipログ前に Windows 0xC0000006 で落ちるケースがある。
#   そのため main.py では DB/cache参照へ進み得る entry/exit schedule job 自体を
#   スケジューラ段階で無効化する。
#
#   対象:
#     - exit_loop_5s
#     - tonosama_entry
#     - ranking_entry
#
# ENV:
#   AUTOSTOCK_MAIN_DISABLE_SCHEDULED_EXIT_LOOP=1   # default in main.py
#   AUTOSTOCK_MAIN_DISABLE_SCHEDULED_ENTRY_JOBS=1  # default in main.py
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


def _disable_exit() -> bool:
    if not _is_main_py_process():
        return False
    return _env_bool("AUTOSTOCK_MAIN_DISABLE_SCHEDULED_EXIT_LOOP", True)


def _disable_entry() -> bool:
    if not _is_main_py_process():
        return False
    return _env_bool("AUTOSTOCK_MAIN_DISABLE_SCHEDULED_ENTRY_JOBS", True)


def _noop_exit_loop(*args, **kwargs):
    logger.info(
        "[MAIN DISABLE ENTRY/EXIT SCHEDULE] skipped exit_loop_5s schedule job in main.py. "
        "Set AUTOSTOCK_MAIN_DISABLE_SCHEDULED_EXIT_LOOP=0 to restore."
    )
    return None


def _noop_tonosama_entry(*args, **kwargs):
    logger.info(
        "[MAIN DISABLE ENTRY/EXIT SCHEDULE] skipped tonosama_entry schedule job in main.py. "
        "Set AUTOSTOCK_MAIN_DISABLE_SCHEDULED_ENTRY_JOBS=0 to restore."
    )
    return 0


def _noop_ranking_entry(*args, **kwargs):
    logger.info(
        "[MAIN DISABLE ENTRY/EXIT SCHEDULE] skipped ranking_entry schedule job in main.py. "
        "Set AUTOSTOCK_MAIN_DISABLE_SCHEDULED_ENTRY_JOBS=0 to restore."
    )
    return 0


def _should_disable_job(tags: set[str], func_name: str) -> tuple[bool, str]:
    """Return (disable, reason)."""
    tags_s = {str(t) for t in tags}
    fn = str(func_name or "")

    if _disable_exit() and ("exit_loop_5s" in tags_s or "exit" in tags_s or "exit_loop" in fn):
        return True, "exit_loop_5s"

    if _disable_entry():
        if "tonosama_entry" in tags_s or "tonosama" in fn:
            return True, "tonosama_entry"
        if "ranking_entry" in tags_s or "ranking_entry" in fn or "_run_ranking_entry" in fn:
            return True, "ranking_entry"

    return False, ""


def _patch_direct_refs() -> int:
    changed = 0

    # exit direct refs
    if _disable_exit():
        try:
            import core.startup.scheduler_exit_bootstrap as boot
            boot.run_exit_loop_market_guarded = _noop_exit_loop
            changed += 1
        except Exception:
            logger.debug("[MAIN DISABLE ENTRY/EXIT SCHEDULE] scheduler_exit_bootstrap patch skipped", exc_info=True)

        try:
            import core.startup.exit_loop_timeout_guard_patch as timeout_guard
            timeout_guard._patched_run_exit_loop_market_guarded = _noop_exit_loop  # type: ignore[attr-defined]
            changed += 1
        except Exception:
            logger.debug("[MAIN DISABLE ENTRY/EXIT SCHEDULE] timeout_guard module patch skipped", exc_info=True)

    # entry direct refs
    if _disable_entry():
        try:
            import trading.entry_exit.tasks as tasks
            if hasattr(tasks, "_run_tonosama_entry_safe"):
                tasks._run_tonosama_entry_safe = _noop_tonosama_entry
                changed += 1
            if hasattr(tasks, "_run_ranking_entry_safe"):
                tasks._run_ranking_entry_safe = _noop_ranking_entry
                changed += 1
        except Exception:
            logger.debug("[MAIN DISABLE ENTRY/EXIT SCHEDULE] entry_exit.tasks patch skipped", exc_info=True)

        try:
            import core.startup.tonosama_skip_build_when_pending_exists_patch as tono
            if hasattr(tono, "patched"):
                tono.patched = _noop_tonosama_entry
                changed += 1
        except Exception:
            logger.debug("[MAIN DISABLE ENTRY/EXIT SCHEDULE] tonosama patch ref skipped", exc_info=True)

        try:
            import core.startup.ranking_stuck_pending_prune_patch as rank
            if hasattr(rank, "patched"):
                rank.patched = _noop_ranking_entry
                changed += 1
        except Exception:
            logger.debug("[MAIN DISABLE ENTRY/EXIT SCHEDULE] ranking patch ref skipped", exc_info=True)

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
                fn_name = ""
                try:
                    fn_name = getattr(fn_obj, "__name__", "") or repr(fn_obj)
                except Exception:
                    fn_name = repr(fn_obj)
                disable, reason = _should_disable_job(tags, fn_name)
                if not disable:
                    continue

                noop = _noop_exit_loop if reason == "exit_loop_5s" else (_noop_tonosama_entry if reason == "tonosama_entry" else _noop_ranking_entry)

                # まず job_func を空処理へ差し替える。cancelに失敗しても安全。
                try:
                    job.job_func = noop
                    replaced += 1
                except Exception:
                    pass

                # 可能なら schedule 自体から削除。
                try:
                    schedule.cancel_job(job)
                    removed += 1
                except Exception:
                    pass

                logger.warning(
                    "[MAIN DISABLE ENTRY/EXIT SCHEDULE] disabled scheduled job reason=%s tags=%s func=%s",
                    reason,
                    sorted(str(t) for t in tags),
                    fn_name,
                )
            except Exception:
                logger.debug("[MAIN DISABLE ENTRY/EXIT SCHEDULE] per-job scan skipped", exc_info=True)
    except Exception:
        logger.debug("[MAIN DISABLE ENTRY/EXIT SCHEDULE] schedule scan skipped", exc_info=True)
    return scanned, removed, replaced


def install() -> bool:
    global _INSTALLED
    # 複数回呼ばれても、後から追加されたschedule jobを消すため scan は毎回行う。
    if not (_disable_exit() or _disable_entry()):
        logger.warning(
            "[MAIN DISABLE ENTRY/EXIT SCHEDULE] install skipped main_py=%s disable_exit=%s disable_entry=%s",
            _is_main_py_process(),
            _disable_exit(),
            _disable_entry(),
        )
        _INSTALLED = True
        return False

    os.environ.setdefault("AUTOSTOCK_MAIN_DISABLE_SCHEDULED_EXIT_LOOP", "1")
    os.environ.setdefault("AUTOSTOCK_MAIN_DISABLE_SCHEDULED_ENTRY_JOBS", "1")

    changed = _patch_direct_refs()
    scanned, removed, replaced = _scan_schedule_jobs()

    _INSTALLED = True
    logger.warning(
        "[MAIN DISABLE ENTRY/EXIT SCHEDULE] installed v2 main_py=True disable_exit=%s disable_entry=%s changed=%s scanned_jobs=%s removed_jobs=%s replaced_jobs=%s",
        _disable_exit(),
        _disable_entry(),
        changed,
        scanned,
        removed,
        replaced,
    )
    return True


try:
    install()
except Exception:
    logger.exception("[MAIN DISABLE ENTRY/EXIT SCHEDULE] auto install failed")


__all__ = ["install"]
