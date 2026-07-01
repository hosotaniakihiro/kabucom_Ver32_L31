# -*- coding: utf-8 -*-
# ============================================================
# File   : core/startup/summary_main_direct_push_force_patch.py
# Version: V2-FORCE-MAIN-DIRECT-PUSH-1M-ROBUST-MEMORY
# ------------------------------------------------------------
# Force main.py 1m summary tick to avoid heavy runner paths.
# V2 uses summary_main_memory_latest_1m_patch._build_memory_1m_summary first.
# If PUSH memory exists but direct build is empty, do not fall back to heavy
# original paths by default.
# ============================================================
from __future__ import annotations

import datetime as dt
import logging
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

import pandas as pd

logger = logging.getLogger(__name__)
VERSION = "V2-FORCE-MAIN-DIRECT-PUSH-1M-ROBUST-MEMORY"
_PATCHED = False
_WATCHER_STARTED = False
_AI_EXECUTOR: ThreadPoolExecutor | None = None
_AI_LOCK = threading.RLock()
_AI_RUNNING: set[str] = set()
_ORIGINAL_JOB_SUMMARY = None


def _env_bool(name: str, default: bool = False) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        s = str(v).strip().lower()
        if s in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}:
            return True
        if s in {"0", "false", "no", "n", "off", "ng", "disable", "disabled"}:
            return False
    except Exception:
        pass
    return bool(default)


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        return int(default) if v is None or str(v).strip() == "" else int(float(v))
    except Exception:
        return int(default)


def _is_main_py() -> bool:
    try:
        argv = " ".join(str(x).replace("\\", "/").lower() for x in sys.argv)
        if any(x in argv for x in ("main_database.py", "data_collectors_runner.py", "summary_database_runner.py", "push_receiver_runner.py")):
            return False
        return "main.py" in argv
    except Exception:
        return False


def _executor() -> ThreadPoolExecutor:
    global _AI_EXECUTOR
    if _AI_EXECUTOR is None:
        _AI_EXECUTOR = ThreadPoolExecutor(max_workers=max(1, _env_int("SUMMARY_FORCE_DIRECT_AI_WORKERS", 1)), thread_name_prefix="summary-force-direct-ai")
    return _AI_EXECUTOR


def _dt_key(now: Any) -> str:
    return now.strftime("%Y%m%d%H%M%S") if isinstance(now, dt.datetime) else str(now)


def _raw_memory_rows() -> int:
    try:
        from core.startup.summary_main_memory_latest_1m_patch import _load_push_memory_df
        df = _load_push_memory_df()
        return len(df) if isinstance(df, pd.DataFrame) else 0
    except Exception:
        return 0


def _build_direct(now: dt.datetime) -> pd.DataFrame:
    t0 = time.perf_counter()
    try:
        from core.startup.summary_main_memory_latest_1m_patch import _build_memory_1m_summary
        df = _build_memory_1m_summary(now=now)
        if isinstance(df, pd.DataFrame) and not df.empty:
            logger.warning(
                "[SUMMARY FORCE DIRECT 1M] built via robust memory rows=%s symbols=%s latest_dt=%s elapsed=%.3fs version=%s",
                len(df),
                int(df["symbol"].nunique()) if "symbol" in df.columns else 0,
                df["datetime"].max() if "datetime" in df.columns else None,
                time.perf_counter() - t0,
                VERSION,
            )
            return df.reset_index(drop=True)
    except Exception:
        logger.debug("[SUMMARY FORCE DIRECT 1M] robust memory builder failed", exc_info=True)
    return pd.DataFrame()


def _store(df: pd.DataFrame) -> None:
    if df is None or df.empty:
        return
    try:
        from global_state import global_data
        for name in ("push_summary_1", "push_summary_1min", "push_merged_summary_1", "push_merged_summary_1min", "merged_summary_1", "merged_summary_1min"):
            try:
                setattr(global_data, name, df)
            except Exception:
                pass
    except Exception:
        pass
    try:
        from core.global_context.context import global_data as GD
        for fn_name in ("set_push_summary", "set_merged_summary", "set_push_merged_summary"):
            fn = getattr(GD, fn_name, None)
            if callable(fn):
                try:
                    if "merged" in fn_name:
                        fn(1, df, source="push")
                    else:
                        fn(1, df)
                except TypeError:
                    try:
                        fn(tf=1, df=df, source="push")
                    except Exception:
                        pass
    except Exception:
        pass


def _submit_ai(df: pd.DataFrame, now: dt.datetime, run_entry: bool) -> None:
    if not run_entry or df is None or df.empty or not _env_bool("SUMMARY_FORCE_DIRECT_ASYNC_AI", True):
        return
    key = "force-summary-ai:1:" + _dt_key(now)
    with _AI_LOCK:
        if key in _AI_RUNNING:
            logger.warning("[SUMMARY FORCE DIRECT 1M] async AI skipped already_running key=%s rows=%s", key, len(df))
            return
        _AI_RUNNING.add(key)
    df_copy = df.copy(deep=False)

    def _task() -> None:
        try:
            logger.warning("[SUMMARY FORCE DIRECT 1M] async AI start key=%s rows=%s", key, len(df_copy))
            try:
                from scheduler_jobs.summary.summary_ai_entry_hook_v20 import run_summary_ai_entry_safe
                run_summary_ai_entry_safe(interval=1, now=now, df=df_copy, source="SUMMARY")
            except Exception:
                logger.exception("[SUMMARY FORCE DIRECT 1M] async AI failed key=%s", key)
        finally:
            with _AI_LOCK:
                _AI_RUNNING.discard(key)
            logger.warning("[SUMMARY FORCE DIRECT 1M] async AI done key=%s", key)

    _executor().submit(_task)
    logger.warning("[SUMMARY FORCE DIRECT 1M] async AI submitted key=%s rows=%s", key, len(df_copy))


def _patch_once(reason: str = "install") -> bool:
    global _ORIGINAL_JOB_SUMMARY
    if not _is_main_py() or not _env_bool("SUMMARY_FORCE_DIRECT_PATCH_ENABLED", True):
        return False
    try:
        import scheduler_jobs.summary.runner_core as rc
        current = getattr(rc, "job_summary", None)
        if getattr(current, "_summary_force_direct_v2", False):
            return True
        if _ORIGINAL_JOB_SUMMARY is None and callable(current):
            _ORIGINAL_JOB_SUMMARY = current
        orig = _ORIGINAL_JOB_SUMMARY if callable(_ORIGINAL_JOB_SUMMARY) else current

        def job_summary_force(interval: int, display: bool = True, now: Optional[dt.datetime] = None, run_entry: bool = True, **kwargs) -> pd.DataFrame:
            interval_i = int(interval)
            now_i = (now or rc.now_naive()).replace(microsecond=0)
            if interval_i != 1:
                return orig(interval_i, display=display, now=now_i, run_entry=run_entry, **kwargs)
            t0 = time.perf_counter()
            df = _build_direct(now_i)
            if df is not None and not df.empty:
                _store(df)
                _submit_ai(df, now_i, run_entry)
                logger.warning("[SUMMARY FORCE DIRECT 1M] return interval=1 rows=%s elapsed=%.3fs mode=forced_direct_v2", len(df), time.perf_counter() - t0)
                return df
            raw_rows = _raw_memory_rows()
            if _env_bool("SUMMARY_FORCE_DIRECT_NO_ORIGINAL_FALLBACK_WHEN_RAW_EXISTS", True) and raw_rows > 0:
                logger.warning(
                    "[SUMMARY FORCE DIRECT 1M] direct empty but raw memory exists -> skip original heavy fallback interval=1 raw_rows=%s elapsed=%.3fs",
                    raw_rows,
                    time.perf_counter() - t0,
                )
                return pd.DataFrame()
            logger.warning("[SUMMARY FORCE DIRECT 1M] direct empty -> original fallback interval=1 raw_rows=%s", raw_rows)
            return orig(interval_i, display=display, now=now_i, run_entry=run_entry, **kwargs)

        job_summary_force._summary_force_direct_v1 = True  # type: ignore[attr-defined]
        job_summary_force._summary_force_direct_v2 = True  # type: ignore[attr-defined]
        job_summary_force._original = orig  # type: ignore[attr-defined]
        rc.job_summary = job_summary_force
        rc.run_push_summary_job = lambda interval=1, display=True, now=None, run_entry=True, **kwargs: job_summary_force(int(interval), display=display, now=now, run_entry=run_entry, **kwargs)
        rc.job_1m = lambda display=True, now=None, run_entry=True: job_summary_force(1, display=display, now=now, run_entry=run_entry)
        logger.warning("[SUMMARY FORCE DIRECT 1M] patched reason=%s target=runner_core.job_summary version=%s", reason, VERSION)
        return True
    except Exception:
        logger.exception("[SUMMARY FORCE DIRECT 1M] patch failed reason=%s", reason)
        return False


def _watcher() -> None:
    deadline = time.time() + max(30, _env_int("SUMMARY_FORCE_DIRECT_WATCH_SEC", 180))
    i = 0
    while time.time() < deadline:
        try:
            _patch_once(reason=f"watcher:{i}")
        except Exception:
            logger.debug("[SUMMARY FORCE DIRECT 1M] watcher reapply failed", exc_info=True)
        i += 1
        time.sleep(max(0.5, float(os.getenv("SUMMARY_FORCE_DIRECT_WATCH_INTERVAL", "2.0"))))
    logger.warning("[SUMMARY FORCE DIRECT 1M] watcher done reapplies=%s", i)


def install() -> bool:
    global _PATCHED, _WATCHER_STARTED
    os.environ.setdefault("SUMMARY_FORCE_DIRECT_NO_ORIGINAL_FALLBACK_WHEN_RAW_EXISTS", "1")
    ok = _patch_once(reason="install")
    if ok and not _WATCHER_STARTED and _env_bool("SUMMARY_FORCE_DIRECT_WATCHER", True):
        _WATCHER_STARTED = True
        threading.Thread(target=_watcher, name="summary-force-direct-1m-watch", daemon=True).start()
        logger.warning("[SUMMARY FORCE DIRECT 1M] watcher started version=%s", VERSION)
    _PATCHED = bool(ok)
    logger.warning("[SUMMARY FORCE DIRECT 1M] installed version=%s ok=%s main=%s", VERSION, ok, _is_main_py())
    return bool(ok)


try:
    install()
except Exception:
    logger.exception("[SUMMARY FORCE DIRECT 1M] auto install failed")

__all__ = ["VERSION", "install"]
