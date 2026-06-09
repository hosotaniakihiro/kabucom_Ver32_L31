# ============================================================
# File   : core/startup/main_skip_ranking_summary_schedule_patch.py
# Version: V1-MAIN-SKIP-RANKING-SUMMARY-SCHEDULE
# ------------------------------------------------------------
# main.py は entry/exit 側プロセスとして動かし、ranking summary の
# DB read/write / Yahoo fill / Discord display は main_database.py 側に寄せる。
#
# 0xC0000006 対策:
#   schedule job tags:ranking_summary_all,startup_scheduler_bootstrap から
#   ranking_summary_schedule_bg_patch が BG thread を submit し、
#   job_ranking_summary_all -> RANKING SUMMARY RUNNER -> NAS SQLite read/write
#   へ進む経路を main.py では入口で止める。
# ============================================================
from __future__ import annotations

import functools
import logging
import os
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)
_INSTALLED = False


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        raw = os.getenv(name)
        if raw is None or str(raw).strip() == "":
            return bool(default)
        s = str(raw).strip().lower()
        if s in {"1", "true", "yes", "y", "on", "enable", "enabled"}:
            return True
        if s in {"0", "false", "no", "n", "off", "disable", "disabled"}:
            return False
    except Exception:
        pass
    return bool(default)


def _is_main_py() -> bool:
    try:
        return Path(sys.argv[0]).name.lower() == "main.py"
    except Exception:
        return False


def _enabled() -> bool:
    return bool(_is_main_py() and _env_bool("AUTOSTOCK_MAIN_SKIP_RANKING_SUMMARY_SCHEDULE", True))


def _skip_ranking_summary_schedule(*args: Any, **kwargs: Any) -> None:
    logger.warning(
        "[MAIN SKIP RANKING SUMMARY SCHEDULE] skipped ranking_summary_all schedule job in main.py "
        "to avoid NAS ranking/summary DB read-write 0xC0000006. "
        "main_database.py handles ranking summary. Set AUTOSTOCK_MAIN_SKIP_RANKING_SUMMARY_SCHEDULE=0 to restore."
    )
    return None


def _replace_schedule_refs() -> int:
    changed = 0
    try:
        import schedule
        for job in list(getattr(schedule, "jobs", []) or []):
            try:
                tags = set(getattr(job, "tags", set()) or set())
                if "ranking_summary_all" not in tags:
                    continue
                jf = getattr(job, "job_func", None)
                if isinstance(jf, functools.partial):
                    job.job_func = functools.partial(_skip_ranking_summary_schedule, *jf.args, **(jf.keywords or {}))
                else:
                    job.job_func = _skip_ranking_summary_schedule
                changed += 1
            except Exception:
                logger.debug("[MAIN SKIP RANKING SUMMARY SCHEDULE] schedule ref replace skipped", exc_info=True)
    except Exception:
        logger.debug("[MAIN SKIP RANKING SUMMARY SCHEDULE] schedule ref replace failed", exc_info=True)
    return changed


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    if not _enabled():
        logger.warning(
            "[MAIN SKIP RANKING SUMMARY SCHEDULE] installed enabled=False main_py=%s",
            _is_main_py(),
        )
        _INSTALLED = True
        return True

    os.environ.setdefault("AUTOSTOCK_MAIN_SKIP_RANKING_SUMMARY_SCHEDULE", "1")
    ok_any = False
    try:
        import core.startup.scheduler_bootstrap as sb
        try:
            sb._run_ranking_summary_all_job_safe = _skip_ranking_summary_schedule
            ok_any = True
        except Exception:
            logger.debug("[MAIN SKIP RANKING SUMMARY SCHEDULE] scheduler_bootstrap target patch skipped", exc_info=True)
    except Exception:
        logger.debug("[MAIN SKIP RANKING SUMMARY SCHEDULE] scheduler_bootstrap import skipped", exc_info=True)

    try:
        import core.startup.ranking_summary_schedule_bg_patch as bg
        try:
            bg._scheduler_bootstrap_job_wrapper = _skip_ranking_summary_schedule
            ok_any = True
        except Exception:
            logger.debug("[MAIN SKIP RANKING SUMMARY SCHEDULE] bg wrapper patch skipped", exc_info=True)
    except Exception:
        logger.debug("[MAIN SKIP RANKING SUMMARY SCHEDULE] bg module import skipped", exc_info=True)

    changed = _replace_schedule_refs()
    _INSTALLED = True
    logger.warning(
        "[MAIN SKIP RANKING SUMMARY SCHEDULE] installed enabled=True main_py=True refs_changed=%s ok_any=%s",
        changed,
        ok_any,
    )
    return True


try:
    install()
except Exception:
    logger.exception("[MAIN SKIP RANKING SUMMARY SCHEDULE] auto install failed")


__all__ = ["install"]
