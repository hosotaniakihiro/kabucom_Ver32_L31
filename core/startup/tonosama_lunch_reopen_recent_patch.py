# ============================================================
# File   : core/startup/tonosama_lunch_reopen_recent_patch.py
# Version: V1-TONOSAMA-LUNCH-REOPEN-RECENT-GRACE
# ------------------------------------------------------------
# Purpose:
#   TONOSAMA volume_surge recent filter normally requires latest 1m/3m/5m
#   summary rows to be within TONOSAMA_RECENT_MAX_AGE_MIN.
#
# Problem:
#   Around lunch reopen, logs showed:
#     now        = 2026-06-01 12:31:57
#     latest_1m  = 2026-06-01 11:30:00
#     age_min    = 61.95
#     after      = 0
#   This is expected if the base summary still only has the morning close
#   row just after 12:30, but the old filter treated it as stale and skipped
#   TONOSAMA entirely:
#     base 1m recent empty -> skip TONOSAMA for safety
#
# Policy:
#   - Only during lunch reopen grace window.
#   - Only when the latest row is around morning close 11:25-11:30.
#   - Reuse a limited morning-close lookback so volume/range features can be
#     computed while the first afternoon bars are still arriving.
#   - Outside the grace window, preserve the original strict stale behavior.
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

_INSTALLED = False
_ORIG_FILTER = None


def _env_bool(name: str, default: bool) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        s = str(v).strip().lower()
        if s in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}:
            return True
        if s in {"0", "false", "no", "n", "off", "disable", "disabled"}:
            return False
        return bool(default)
    except Exception:
        return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _hm_to_minutes(text: str, default: int) -> int:
    try:
        h, m = str(text).strip().split(":", 1)
        return int(h) * 60 + int(m)
    except Exception:
        return int(default)


def _minutes_of_day(t: dt.datetime) -> int:
    return int(t.hour) * 60 + int(t.minute)


def _patched_filter_recent_rows(df: pd.DataFrame, *, interval: int, label: str) -> pd.DataFrame:
    # First keep original behavior.  We only rescue when original returns empty.
    try:
        out = _ORIG_FILTER(df, interval=interval, label=label)
        if out is not None and not out.empty:
            return out
    except Exception:
        logger.debug("[TONOSAMA LUNCH REOPEN RECENT] original filter failed", exc_info=True)

    if not _env_bool("TONOSAMA_LUNCH_REOPEN_RECENT_GRACE", True):
        return out if isinstance(out, pd.DataFrame) else pd.DataFrame()

    try:
        import trading.entry.tonosama.volume_surge as vs

        if df is None or df.empty or "datetime" not in df.columns:
            return out if isinstance(out, pd.DataFrame) else pd.DataFrame()

        x0 = vs._normalize_datetime_col(df)  # type: ignore[attr-defined]
        if x0.empty:
            return out if isinstance(out, pd.DataFrame) else pd.DataFrame()

        now = vs._now_naive()  # type: ignore[attr-defined]
        now_min = _minutes_of_day(now)
        reopen_start = _hm_to_minutes(os.getenv("TONOSAMA_LUNCH_REOPEN_START", "12:30"), 12 * 60 + 30)
        reopen_grace_min = max(1.0, _env_float("TONOSAMA_LUNCH_REOPEN_GRACE_MIN", 20.0))
        reopen_end = reopen_start + int(reopen_grace_min)
        if not (reopen_start <= now_min <= reopen_end):
            return out if isinstance(out, pd.DataFrame) else pd.DataFrame()

        latest = pd.to_datetime(x0["datetime"], errors="coerce").max()
        if pd.isna(latest):
            return out if isinstance(out, pd.DataFrame) else pd.DataFrame()
        latest_py = latest.to_pydatetime().replace(tzinfo=None)
        if latest_py.date() != now.date():
            return out if isinstance(out, pd.DataFrame) else pd.DataFrame()

        morning_close_start = _hm_to_minutes(os.getenv("TONOSAMA_LUNCH_MORNING_CLOSE_START", "11:25"), 11 * 60 + 25)
        morning_close_end = _hm_to_minutes(os.getenv("TONOSAMA_LUNCH_MORNING_CLOSE_END", "11:30"), 11 * 60 + 30)
        latest_min = _minutes_of_day(latest_py)
        if not (morning_close_start <= latest_min <= morning_close_end):
            return out if isinstance(out, pd.DataFrame) else pd.DataFrame()

        lookback_min = max(5.0, _env_float("TONOSAMA_LUNCH_REOPEN_LOOKBACK_MIN", 45.0))
        cutoff = pd.Timestamp(latest_py - dt.timedelta(minutes=lookback_min))
        today = pd.Timestamp(now.date())
        rescued = x0[(x0["datetime"] >= cutoff) & (x0["datetime"] >= today)].copy()
        if rescued.empty:
            return out if isinstance(out, pd.DataFrame) else pd.DataFrame()

        logger.warning(
            "[TONOSAMA LUNCH REOPEN RECENT] rescue label=%s interval=%s rows=%s cutoff=%s latest=%s now=%s grace_min=%.1f lookback_min=%.1f source_counts=%s",
            label,
            interval,
            len(rescued),
            cutoff,
            latest_py,
            now,
            reopen_grace_min,
            lookback_min,
            vs._source_counts(rescued) if hasattr(vs, "_source_counts") else {},
        )
        return rescued
    except Exception:
        logger.exception("[TONOSAMA LUNCH REOPEN RECENT] rescue failed label=%s interval=%s", label, interval)
        return out if isinstance(out, pd.DataFrame) else pd.DataFrame()


def install() -> bool:
    global _INSTALLED, _ORIG_FILTER
    if _INSTALLED:
        return True
    try:
        import trading.entry.tonosama.volume_surge as vs

        cur = getattr(vs, "_filter_recent_rows", None)
        if not callable(cur):
            logger.warning("[TONOSAMA LUNCH REOPEN RECENT] target missing")
            return False
        if getattr(cur, "_tonosama_lunch_reopen_recent_patch", False):
            _INSTALLED = True
            return True

        _ORIG_FILTER = cur
        _patched_filter_recent_rows._tonosama_lunch_reopen_recent_patch = True  # type: ignore[attr-defined]
        _patched_filter_recent_rows._original = cur  # type: ignore[attr-defined]
        vs._filter_recent_rows = _patched_filter_recent_rows

        _INSTALLED = True
        logger.warning(
            "[TONOSAMA LUNCH REOPEN RECENT] installed enabled=%s grace_min=%s lookback_min=%s",
            _env_bool("TONOSAMA_LUNCH_REOPEN_RECENT_GRACE", True),
            os.getenv("TONOSAMA_LUNCH_REOPEN_GRACE_MIN", "20"),
            os.getenv("TONOSAMA_LUNCH_REOPEN_LOOKBACK_MIN", "45"),
        )
        return True
    except Exception:
        logger.exception("[TONOSAMA LUNCH REOPEN RECENT] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[TONOSAMA LUNCH REOPEN RECENT] auto install failed")


__all__ = ["install"]
