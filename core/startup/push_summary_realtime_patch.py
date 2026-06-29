# ============================================================
# File   : core/startup/push_summary_realtime_patch.py
# Version: REV3-PUSH-SUMMARY-REALTIME-MTF-BOOTSTRAP
# ------------------------------------------------------------
# PUSH DB flush後にsummaryを軽く再計算するruntime patch。
# 旧版は1分足のみが既定だったため、3分足/5分足が空または古いままになり、
# TONOSAMA/ENTRY側で tf=3 source=push rows=0 になっていた。
#
# REV3:
#   - 既定の再計算対象を 1,3,5 に変更。
#   - flush が発生しない memory_only 側や起動直後でも、PUSH/summary関係プロセスでは
#     起動後に一度だけ 1,3,5 を再計算する bootstrap を追加。
#   - rebuild は scheduler_jobs.summary.runner_core.job_summary に統一し、
#     push_summary_engine の module/function 解決差異に依存しない。
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import sys
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
_DEFAULT_INTERVALS = (1, 3, 5)


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


def _parse_intervals(value: str | None, default: Iterable[int] = _DEFAULT_INTERVALS) -> list[int]:
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


def _argv_text() -> str:
    try:
        return " ".join(str(x).replace("\\", "/").lower() for x in sys.argv)
    except Exception:
        return ""


def _should_bootstrap_rebuild_here() -> bool:
    """起動直後の空/古いMTF補完はPUSH/summaryに関係するDB系プロセスだけで行う。"""
    if not _env_bool("PUSH_SUMMARY_REALTIME_BOOTSTRAP_REBUILD_ENABLED", True):
        return False
    if _env_bool("PUSH_SUMMARY_REALTIME_BOOTSTRAP_FORCE", False):
        return True
    argv = _argv_text()
    return any(x in argv for x in (
        "main_database.py",
        "push_receiver_runner.py",
        "summary_database_runner.py",
    ))


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
    """runner_core.job_summary を使って、DB保存経路と同じ処理でsummaryを作る。"""
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


def _trigger_summary_rebuild(reason: str, *, ignore_cooldown: bool = False) -> None:
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

    intervals = _parse_intervals(os.getenv("PUSH_SUMMARY_REALTIME_INTERVALS"), default=_default_intervals())
    logger.warning("[PUSH SUMMARY REALTIME] rebuild queued intervals=%s reason=%s", intervals, reason)
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
        if getattr(original, "_push_summary_realtime_patched_v3", False):
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
                    else:
                        logger.debug("[PUSH SUMMARY REALTIME] flush ok but no row delta rows=%s delta=%s", rows, delta)
            except Exception:
                logger.debug("[PUSH SUMMARY REALTIME] flush post-trigger failed", exc_info=True)
            return ok

        _flush_with_realtime_summary._push_summary_realtime_patched_v3 = True  # type: ignore[attr-defined]
        setattr(cls, "flush", _flush_with_realtime_summary)
        return True
    except Exception:
        logger.exception("[PUSH SUMMARY REALTIME] patch push_db_writer failed")
        return False


def _schedule_bootstrap_rebuild() -> None:
    if not _should_bootstrap_rebuild_here():
        return
    delay = _env_float("PUSH_SUMMARY_REALTIME_BOOTSTRAP_DELAY_SEC", 3.0)

    def _delayed_bootstrap() -> None:
        try:
            if delay > 0:
                time.sleep(delay)
            _trigger_summary_rebuild(reason="install_bootstrap", ignore_cooldown=True)
        except Exception:
            logger.debug("[PUSH SUMMARY REALTIME] bootstrap trigger failed", exc_info=True)

    threading.Thread(target=_delayed_bootstrap, daemon=True, name="PushSummaryRealtimeBootstrap").start()


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
    intervals = _parse_intervals(os.getenv("PUSH_SUMMARY_REALTIME_INTERVALS"), default=_default_intervals())
    bootstrap = _should_bootstrap_rebuild_here()
    logger.warning(
        "[PUSH SUMMARY REALTIME] installed ok=%s engine_db_fallback=%s flush_trigger=%s intervals=%s bootstrap=%s version=REV3 argv=%s",
        _PATCHED,
        False,
        ok_writer,
        intervals,
        bootstrap,
        sys.argv,
    )
    if _PATCHED and bootstrap:
        _schedule_bootstrap_rebuild()
    return _PATCHED


__all__ = ["install"]
