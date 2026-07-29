# ============================================================
# File   : trading/push/push_summary_rebuild_trigger.py
# ------------------------------------------------------------
# PUSH DB flush後にsummaryを軽く再計算するトリガー。
# 旧 core/startup/push_summary_realtime_patch.py から移設。
#
# StreamDBWriter.flush() が新規行を保存できたときに呼ぶ
# trigger_summary_rebuild()、main_database.py 起動直後に一度だけ
# 呼ぶ schedule_bootstrap_rebuild() を提供する。
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import threading
import time
from typing import Any, Iterable, Optional

import pandas as pd

logger = logging.getLogger(__name__)

_TRIGGER_LOCK = threading.RLock()
_TRIGGER_RUNNING = False
_LAST_TRIGGER_AT = 0.0
_DEFAULT_INTERVALS = (1, 3, 5)
_BOOTSTRAP_DEFAULT_INTERVALS = (1,)


def _env_bool(name: str, default: bool) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).strip())
    except Exception:
        return float(default)


def _parse_intervals(value: str | None, default: Iterable[int]) -> list[int]:
    if not value or not str(value).strip():
        return [int(x) for x in default]
    out: list[int] = []
    for part in str(value).replace(";", ",").split(","):
        s = part.strip().lower().replace("min", "").replace("m", "")
        if not s:
            continue
        try:
            n = int(float(s))
            if n > 0 and n not in out:
                out.append(n)
        except Exception:
            pass
    return out or [int(x) for x in default]


def _default_intervals() -> list[int]:
    return _parse_intervals(os.getenv("PUSH_SUMMARY_REALTIME_DEFAULT_INTERVALS"), default=_DEFAULT_INTERVALS)


def _bootstrap_intervals() -> list[int]:
    return _parse_intervals(os.getenv("PUSH_SUMMARY_REALTIME_BOOTSTRAP_INTERVALS"), default=_BOOTSTRAP_DEFAULT_INTERVALS)


def _safe_latest_dt(df: Any) -> Optional[pd.Timestamp]:
    try:
        if not isinstance(df, pd.DataFrame) or df.empty:
            return None
        for col in ("datetime", "end_time", "start_time", "received_at", "time"):
            if col not in df.columns:
                continue
            s = pd.to_datetime(df[col], errors="coerce")
            if s.notna().any():
                ts = s.max()
                try:
                    ts = ts.tz_localize(None)
                except Exception:
                    pass
                return ts
    except Exception:
        pass
    return None


def _safe_symbol_count(df: Any) -> int:
    try:
        if not isinstance(df, pd.DataFrame) or df.empty or "symbol" not in df.columns:
            return 0
        return int(df["symbol"].astype(str).nunique())
    except Exception:
        return 0


def _build_summary(interval: int) -> pd.DataFrame:
    try:
        from scheduler_jobs.summary.runner_core import job_summary
        n = dt.datetime.now().replace(second=0, microsecond=0)
        df = job_summary(int(interval), display=False, now=n, run_entry=False)
        return df if isinstance(df, pd.DataFrame) else pd.DataFrame()
    except Exception:
        logger.exception("[PUSH SUMMARY REALTIME] runner_core job_summary failed interval=%s", interval)
        return pd.DataFrame()


def _summary_rebuild_worker(intervals: list[int], reason: str) -> None:
    global _TRIGGER_RUNNING, _LAST_TRIGGER_AT
    try:
        for interval in intervals:
            try:
                logger.warning("[PUSH SUMMARY REALTIME] rebuild start interval=%s reason=%s", interval, reason)
                df = _build_summary(int(interval))
                logger.warning(
                    "[PUSH SUMMARY REALTIME] rebuild done interval=%s rows=%s symbols=%s latest_dt=%s reason=%s",
                    interval,
                    len(df) if isinstance(df, pd.DataFrame) else 0,
                    _safe_symbol_count(df),
                    _safe_latest_dt(df),
                    reason,
                )
            except Exception:
                logger.exception("[PUSH SUMMARY REALTIME] rebuild failed interval=%s reason=%s", interval, reason)
    finally:
        with _TRIGGER_LOCK:
            _TRIGGER_RUNNING = False
            _LAST_TRIGGER_AT = time.time()


def trigger_summary_rebuild(reason: str, *, ignore_cooldown: bool = False, intervals_override: list[int] | None = None) -> None:
    global _TRIGGER_RUNNING, _LAST_TRIGGER_AT
    if not _env_bool("PUSH_SUMMARY_REALTIME_REBUILD_ENABLED", True):
        return
    cooldown = _env_float("PUSH_SUMMARY_REALTIME_COOLDOWN_SEC", 20.0)
    now = time.time()
    with _TRIGGER_LOCK:
        if _TRIGGER_RUNNING:
            logger.debug("[PUSH SUMMARY REALTIME] rebuild skipped already running reason=%s", reason)
            return
        if not ignore_cooldown and now - float(_LAST_TRIGGER_AT or 0.0) < cooldown:
            logger.debug("[PUSH SUMMARY REALTIME] rebuild skipped cooldown reason=%s", reason)
            return
        _TRIGGER_RUNNING = True

    intervals = list(intervals_override or _parse_intervals(os.getenv("PUSH_SUMMARY_REALTIME_INTERVALS"), default=_default_intervals()))
    logger.warning("[PUSH SUMMARY REALTIME] rebuild queued intervals=%s reason=%s", intervals, reason)
    th = threading.Thread(target=_summary_rebuild_worker, args=(intervals, reason), daemon=True, name="PushSummaryRealtimeRebuild")
    th.start()


def schedule_bootstrap_rebuild() -> None:
    """main_database.py 起動直後に一度だけ呼ぶ。1m summaryをbootstrap再計算する。"""
    if not _env_bool("PUSH_SUMMARY_REALTIME_BOOTSTRAP_REBUILD_ENABLED", True):
        return
    delay = _env_float("PUSH_SUMMARY_REALTIME_BOOTSTRAP_DELAY_SEC", 3.0)
    intervals = _bootstrap_intervals()

    def _delayed_bootstrap() -> None:
        try:
            if delay > 0:
                time.sleep(delay)
            trigger_summary_rebuild(reason="install_bootstrap", ignore_cooldown=True, intervals_override=intervals)
        except Exception:
            logger.debug("[PUSH SUMMARY REALTIME] bootstrap trigger failed", exc_info=True)

    logger.warning("[PUSH SUMMARY REALTIME] bootstrap scheduled intervals=%s delay=%.1fs", intervals, delay)
    threading.Thread(target=_delayed_bootstrap, daemon=True, name="PushSummaryRealtimeBootstrap").start()


__all__ = ["trigger_summary_rebuild", "schedule_bootstrap_rebuild"]
