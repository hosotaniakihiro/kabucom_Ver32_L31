# ============================================================
# File   : core/startup/push_summary_realtime_patch.py
# Version: REV2-PUSH-SUMMARY-REALTIME-RUNNER-CORE
# ------------------------------------------------------------
# PUSH DB flush後に1分足summaryを軽く再計算するruntime patch。
# 旧版は環境によって push_summary_engine が function として解決され、
# AttributeError: 'function' object has no attribute 'build_summary'
# で失敗していた。
#
# REV2では rebuild を scheduler_jobs.summary.runner_core.job_summary に統一し、
# engineのmodule/function解決差異に依存しない。
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

_PATCHED = False
_ORIGINAL_STREAM_WRITER_FLUSH = None
_TRIGGER_LOCK = threading.RLock()
_TRIGGER_RUNNING = False
_LAST_TRIGGER_AT = 0.0


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


def _parse_intervals(value: str | None, default: Iterable[int] = (1,)) -> list[int]:
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
    """runner_core.job_summary を使って、DB保存経路と同じ処理で1m summaryを作る。"""
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


def _trigger_summary_rebuild(reason: str) -> None:
    global _TRIGGER_RUNNING, _LAST_TRIGGER_AT
    if not _env_bool("PUSH_SUMMARY_REALTIME_REBUILD_ENABLED", True):
        return

    cooldown = _env_float("PUSH_SUMMARY_REALTIME_COOLDOWN_SEC", 20.0)
    now = time.time()
    with _TRIGGER_LOCK:
        if _TRIGGER_RUNNING:
            logger.debug("[PUSH SUMMARY REALTIME] rebuild skipped already running reason=%s", reason)
            return
        if now - float(_LAST_TRIGGER_AT or 0.0) < cooldown:
            logger.debug("[PUSH SUMMARY REALTIME] rebuild skipped cooldown reason=%s", reason)
            return
        _TRIGGER_RUNNING = True

    intervals = _parse_intervals(os.getenv("PUSH_SUMMARY_REALTIME_INTERVALS"), default=(1,))
    th = threading.Thread(
        target=_summary_rebuild_worker,
        args=(intervals, reason),
        daemon=True,
        name="PushSummaryRealtimeRebuild",
    )
    th.start()


def _patch_push_db_writer() -> bool:
    global _ORIGINAL_STREAM_WRITER_FLUSH
    try:
        import trading.push.push_db_writer as writer_mod

        cls = getattr(writer_mod, "StreamDBWriter", None)
        original = getattr(cls, "flush", None) if cls is not None else None
        if not callable(original):
            logger.warning("[PUSH SUMMARY REALTIME] StreamDBWriter.flush not found")
            return False
        if getattr(original, "_push_summary_realtime_patched_v2", False):
            return True

        _ORIGINAL_STREAM_WRITER_FLUSH = original

        def _flush_with_realtime_summary(self, *args, **kwargs):
            ok = original(self, *args, **kwargs)
            try:
                if bool(ok):
                    delta = int(getattr(writer_mod.global_data, "last_flush_delta", 0) or 0)
                    rows = int(getattr(writer_mod.global_data, "last_flush_rows", 0) or 0)
                    if delta > 0 or rows > 0:
                        _trigger_summary_rebuild(reason=f"push_flush rows={rows} delta={delta}")
            except Exception:
                logger.debug("[PUSH SUMMARY REALTIME] flush post-trigger failed", exc_info=True)
            return ok

        _flush_with_realtime_summary._push_summary_realtime_patched_v2 = True  # type: ignore[attr-defined]
        setattr(cls, "flush", _flush_with_realtime_summary)
        return True
    except Exception:
        logger.exception("[PUSH SUMMARY REALTIME] patch push_db_writer failed")
        return False


def install() -> bool:
    global _PATCHED
    if _PATCHED:
        return True
    if not _env_bool("PUSH_SUMMARY_REALTIME_PATCH_ENABLED", True):
        logger.warning("[PUSH SUMMARY REALTIME] disabled by env")
        return False

    # 互換用。存在すれば function import 環境にも build_summary を付与する。
    try:
        from core.startup import push_summary_realtime_callable_fix_patch
        push_summary_realtime_callable_fix_patch.install()
    except Exception:
        logger.debug("[PUSH SUMMARY REALTIME] callable fix optional install failed", exc_info=True)

    ok_writer = _patch_push_db_writer()
    _PATCHED = bool(ok_writer)
    logger.warning(
        "[PUSH SUMMARY REALTIME] installed ok=%s engine_db_fallback=%s flush_trigger=%s intervals=%s version=REV2",
        _PATCHED,
        False,
        ok_writer,
        _parse_intervals(os.getenv("PUSH_SUMMARY_REALTIME_INTERVALS"), default=(1,)),
    )
    return _PATCHED


__all__ = ["install"]
