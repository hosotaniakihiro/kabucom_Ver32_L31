# ============================================================
# File   : core/startup/scheduler_helpers.py
# Version: FINAL-PRODUCTION-REV1.0-SCHEDULER-HELPERS
# ------------------------------------------------------------
# 【概要】
#   schedule.jobs の snapshot / tag重複確認などの共通補助。
# ============================================================

from __future__ import annotations

import logging
from typing import Any

import schedule

logger = logging.getLogger(__name__)


def safe_schedule_snapshot(limit: int = 50) -> list[dict[str, Any]]:
    snapshot: list[dict[str, Any]] = []

    try:
        jobs = list(getattr(schedule, "jobs", []) or [])
    except Exception:
        return snapshot

    for j in jobs[: int(limit)]:
        try:
            snapshot.append(
                {
                    "job": repr(j),
                    "tags": sorted([str(x) for x in (getattr(j, "tags", set()) or set())]),
                    "next_run": str(getattr(j, "next_run", None)),
                    "last_run": str(getattr(j, "last_run", None)),
                    "interval": str(getattr(j, "interval", None)),
                    "unit": str(getattr(j, "unit", None)),
                }
            )
        except Exception:
            try:
                snapshot.append({"job": repr(j)})
            except Exception:
                snapshot.append({"job": "<unrepresentable>"})

    return snapshot


def log_scheduler_snapshot(context: str, *, limit: int = 50) -> None:
    try:
        jobs = list(getattr(schedule, "jobs", []) or [])
    except Exception:
        jobs = []

    logger.info(
        "[scheduler_startup][SCHEDULER SNAPSHOT] context=%s jobs=%s snapshot=%s",
        context,
        len(jobs),
        safe_schedule_snapshot(limit=limit),
    )


def has_schedule_tag(tag: str) -> bool:
    try:
        for job in list(getattr(schedule, "jobs", []) or []):
            tags = getattr(job, "tags", set()) or set()
            if str(tag) in {str(x) for x in tags}:
                return True
    except Exception:
        logger.warning("[scheduler_startup] schedule tag check failed tag=%s", tag, exc_info=True)

    return False


__all__ = [
    "safe_schedule_snapshot",
    "log_scheduler_snapshot",
    "has_schedule_tag",
]
