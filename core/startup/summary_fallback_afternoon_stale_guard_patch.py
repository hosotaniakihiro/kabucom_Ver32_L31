# ============================================================
# File   : core/startup/summary_fallback_afternoon_stale_guard_patch.py
# Version: V1-AFTERNOON-LUNCH-FALLBACK-SUPPRESS
# ------------------------------------------------------------
# Purpose:
#   After 12:30, do not treat 11:30 / morning 5m or 3m fallback summaries
#   as fresh enough for live operation.  This prevents logs like:
#     latest_dt=11:30 age_min=30.00 fresh=True expected_slot=12:00
#   from being selected after the afternoon session has opened.
# ============================================================
from __future__ import annotations

import datetime as dt
import logging
import os
from functools import wraps
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)
_INSTALLED = False
_ORIG_SELECT = None


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _actual_now() -> dt.datetime:
    return dt.datetime.now().replace(tzinfo=None, microsecond=0)


def _is_afternoon_wall_clock() -> bool:
    n = _actual_now()
    if n.weekday() >= 5:
        return False
    return dt.time(12, 30) <= n.time() <= dt.time(15, 30)


def _latest_ts(df: pd.DataFrame):
    try:
        from scheduler_jobs.summary.display_prepare import extract_latest_timestamp
        return extract_latest_timestamp(df)
    except Exception:
        pass
    for col in ("datetime", "end_time", "time", "start_time", "snapshot_time"):
        if col in getattr(df, "columns", []):
            try:
                s = pd.to_datetime(df[col], errors="coerce")
                return s.max()
            except Exception:
                continue
    return None


def _is_morning_or_lunch_ts(ts: Any) -> bool:
    try:
        if ts is None or pd.isna(ts):
            return False
        t = pd.Timestamp(ts).to_pydatetime().replace(tzinfo=None).time()
        return t < dt.time(12, 30)
    except Exception:
        return False


def install() -> bool:
    global _INSTALLED, _ORIG_SELECT
    if _INSTALLED:
        return True
    try:
        os.environ.setdefault("SUMMARY_SUPPRESS_LUNCH_FALLBACK_AFTER_PM", "1")

        import scheduler_jobs.summary.fallback_loader as fl
        cur = getattr(fl, "select_best_candidate", None)
        if not callable(cur):
            logger.warning("[SUMMARY AFTERNOON STALE GUARD] target missing")
            return False
        if getattr(cur, "_summary_afternoon_stale_guard_v1", False):
            _INSTALLED = True
            return True

        _ORIG_SELECT = cur

        @wraps(cur)
        def wrapped_select_best_candidate(candidates: list[tuple[str, pd.DataFrame]], *args: Any, **kwargs: Any):
            try:
                interval = int(kwargs.get("interval", args[0] if args else 1))
            except Exception:
                interval = 1

            if (
                _env_bool("SUMMARY_SUPPRESS_LUNCH_FALLBACK_AFTER_PM", True)
                and interval >= 3
                and _is_afternoon_wall_clock()
            ):
                filtered: list[tuple[str, pd.DataFrame]] = []
                dropped: list[tuple[str, str]] = []
                for name, df in candidates or []:
                    ts = _latest_ts(df)
                    if _is_morning_or_lunch_ts(ts):
                        dropped.append((str(name), str(ts)))
                        continue
                    filtered.append((name, df))

                if dropped:
                    logger.warning(
                        "[SUMMARY AFTERNOON STALE GUARD] dropped morning/lunch fallback after PM interval=%s dropped=%s kept=%s",
                        interval,
                        dropped[:10],
                        len(filtered),
                    )
                candidates = filtered

            return _ORIG_SELECT(candidates, *args, **kwargs)

        wrapped_select_best_candidate._summary_afternoon_stale_guard_v1 = True  # type: ignore[attr-defined]
        wrapped_select_best_candidate._original = _ORIG_SELECT  # type: ignore[attr-defined]
        fl.select_best_candidate = wrapped_select_best_candidate

        _INSTALLED = True
        logger.warning(
            "[SUMMARY AFTERNOON STALE GUARD] installed enabled=%s",
            os.getenv("SUMMARY_SUPPRESS_LUNCH_FALLBACK_AFTER_PM"),
        )
        return True
    except Exception:
        logger.exception("[SUMMARY AFTERNOON STALE GUARD] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[SUMMARY AFTERNOON STALE GUARD] auto install failed")

__all__ = ["install"]
