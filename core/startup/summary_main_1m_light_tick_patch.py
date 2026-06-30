# -*- coding: utf-8 -*-
# ============================================================
# File   : core/startup/summary_main_1m_light_tick_patch.py
# Version: V2-MAIN-1M-SUMMARY-EARLY-ASYNC-AI
# ------------------------------------------------------------
# Purpose:
#   Keep main.py summary_parent_tick responsive.
#
#   In main.py, entry judgement should not wait for heavy display/enrich/Discord
#   processing.  Submit Summary-AI immediately after the PUSH df is obtained and
#   normalized enough for candidate selection; then return the df quickly.
#
#   main_database.py remains the owner of DB persistence.
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
VERSION = "V2-MAIN-1M-SUMMARY-EARLY-ASYNC-AI"
_INSTALLED = False
_AI_EXECUTOR: ThreadPoolExecutor | None = None
_AI_LOCK = threading.RLock()
_AI_RUNNING_KEYS: set[str] = set()


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
        return bool(default)
    except Exception:
        return bool(default)


def _is_main_py() -> bool:
    try:
        argv = " ".join(str(x).replace("\\", "/").lower() for x in sys.argv)
        if any(x in argv for x in ("main_database.py", "data_collectors_runner.py", "summary_database_runner.py", "push_receiver_runner.py")):
            return False
        return "main.py" in argv
    except Exception:
        return False


def _is_entry_only_context() -> bool:
    try:
        role = str(os.getenv("SUMMARY_DB_WRITER_ROLE") or "").strip().lower()
        return _is_main_py() or _env_bool("SUMMARY_MAIN_ENTRY_ONLY", False) or role in {"entry_only", "main_entry_only", "read_only", "no_save"}
    except Exception:
        return _is_main_py()


def _executor() -> ThreadPoolExecutor:
    global _AI_EXECUTOR
    if _AI_EXECUTOR is None:
        workers = 1
        try:
            workers = max(1, int(float(os.getenv("SUMMARY_MAIN_ASYNC_AI_WORKERS", "1"))))
        except Exception:
            workers = 1
        _AI_EXECUTOR = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="summary-main-ai-async")
    return _AI_EXECUTOR


def _dt_key(now: Any) -> str:
    try:
        if isinstance(now, dt.datetime):
            return now.strftime("%Y%m%d%H%M%S")
        return str(now)
    except Exception:
        return "unknown"


def _normalize_dt_naive_series(s: Any) -> Any:
    try:
        return pd.to_datetime(s, errors="coerce").dt.tz_localize(None)
    except Exception:
        try:
            return pd.to_datetime(s, errors="coerce", utc=True).dt.tz_convert(None)
        except Exception:
            return s


def _normalize_df_light(df: pd.DataFrame, *, interval: int, now: dt.datetime) -> pd.DataFrame:
    try:
        if not isinstance(df, pd.DataFrame) or df.empty:
            return pd.DataFrame()
        out = df.copy(deep=False)
        if "datetime" in out.columns:
            out["datetime"] = _normalize_dt_naive_series(out["datetime"])
        if "symbol" in out.columns:
            out["symbol"] = out["symbol"].astype(str).str.strip()
            out = out[out["symbol"].ne("")]
        # Future row guard without expensive display/enrich path.
        if "datetime" in out.columns:
            try:
                cutoff = pd.Timestamp(now).tz_localize(None)
                out = out[out["datetime"].isna() | (out["datetime"] <= cutoff)]
            except Exception:
                pass
        return out.reset_index(drop=True)
    except Exception:
        logger.exception("[SUMMARY MAIN LIGHT TICK] light normalize failed interval=%s", interval)
        return df if isinstance(df, pd.DataFrame) else pd.DataFrame()


def _submit_async_ai(df: pd.DataFrame, *, interval: int, now: dt.datetime, run_entry: bool, reason: str) -> None:
    if not (_is_entry_only_context() and _env_bool("SUMMARY_MAIN_ASYNC_AI_ENTRY", True)):
        return
    if not (run_entry and int(interval) in (1, 3, 5)):
        logger.info(
            "[SUMMARY MAIN LIGHT TICK] async AI skipped interval=%s run_entry=%s reason=%s",
            interval,
            run_entry,
            "interval_not_enabled" if int(interval) not in (1, 3, 5) else "run_entry_false",
        )
        return
    rows = len(df) if isinstance(df, pd.DataFrame) else 0
    if rows <= 0:
        logger.warning("[SUMMARY MAIN LIGHT TICK] async AI skipped empty interval=%s reason=%s", interval, reason)
        return
    key = f"summary-ai:{int(interval)}:{_dt_key(now)}"
    with _AI_LOCK:
        if key in _AI_RUNNING_KEYS:
            logger.warning("[SUMMARY MAIN LIGHT TICK] async AI skipped already_running key=%s rows=%s reason=%s", key, rows, reason)
            return
        _AI_RUNNING_KEYS.add(key)

    df_copy = df.copy(deep=False) if isinstance(df, pd.DataFrame) else df

    def _task() -> None:
        try:
            logger.warning("[SUMMARY MAIN LIGHT TICK] async AI start key=%s interval=%s rows=%s now=%s reason=%s", key, interval, rows, now, reason)
            try:
                from scheduler_jobs.summary.summary_ai_entry_hook_v20 import run_summary_ai_entry_safe
                run_summary_ai_entry_safe(interval=int(interval), now=now, df=df_copy, source="SUMMARY")
            except Exception:
                logger.exception("[SUMMARY MAIN LIGHT TICK] async AI failed key=%s interval=%s", key, interval)
        finally:
            with _AI_LOCK:
                _AI_RUNNING_KEYS.discard(key)
            logger.warning("[SUMMARY MAIN LIGHT TICK] async AI done key=%s interval=%s", key, interval)

    _executor().submit(_task)
    logger.warning("[SUMMARY MAIN LIGHT TICK] async AI submitted key=%s interval=%s rows=%s now=%s reason=%s", key, interval, rows, now, reason)


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    if not _is_entry_only_context():
        logger.warning("[SUMMARY MAIN LIGHT TICK] skipped non-main context version=%s", VERSION)
        return False

    try:
        os.environ.setdefault("SUMMARY_MAIN_ASYNC_AI_ENTRY", "1")
        os.environ.setdefault("SUMMARY_MAIN_SKIP_SYNC_SAVE", "1")
        os.environ.setdefault("SUMMARY_MAIN_ASYNC_AI_WORKERS", "1")
        os.environ.setdefault("SUMMARY_MAIN_EARLY_RETURN_AFTER_AI_SUBMIT", "1")
        os.environ.setdefault("SUMMARY_MAIN_LIGHT_DISPLAY", "0")

        import scheduler_jobs.summary.runner_core as rc

        orig_job_summary = getattr(rc, "job_summary", None)
        orig_ai = getattr(rc, "_run_push_ai_entry_before_display", None)
        orig_save = getattr(rc, "_save_summary_if_owner", None)

        if callable(orig_save) and not getattr(orig_save, "_main_light_tick_wrapped", False):
            def _save_summary_if_owner_light(df: pd.DataFrame, interval: int, *, source: str) -> None:
                if _is_entry_only_context() and _env_bool("SUMMARY_MAIN_SKIP_SYNC_SAVE", True):
                    logger.warning(
                        "[SUMMARY MAIN LIGHT TICK] sync save skipped in main interval=%s source=%s rows=%s reason=database_owner_main_database",
                        interval,
                        source,
                        len(df) if isinstance(df, pd.DataFrame) else 0,
                    )
                    return None
                return orig_save(df, interval, source=source)

            _save_summary_if_owner_light._main_light_tick_wrapped = True  # type: ignore[attr-defined]
            _save_summary_if_owner_light._original = orig_save  # type: ignore[attr-defined]
            rc._save_summary_if_owner = _save_summary_if_owner_light

        if callable(orig_ai) and not getattr(orig_ai, "_main_light_tick_wrapped", False):
            def _run_push_ai_entry_before_display_light(df: pd.DataFrame, interval: int, now: dt.datetime, run_entry: bool) -> None:
                _submit_async_ai(df, interval=int(interval), now=now, run_entry=run_entry, reason="original_ai_hook_async")
                return None

            _run_push_ai_entry_before_display_light._main_light_tick_wrapped = True  # type: ignore[attr-defined]
            _run_push_ai_entry_before_display_light._original = orig_ai  # type: ignore[attr-defined]
            rc._run_push_ai_entry_before_display = _run_push_ai_entry_before_display_light

        if callable(orig_job_summary) and not getattr(orig_job_summary, "_main_light_tick_job_wrapped", False):
            def job_summary_light(interval: int, display: bool = True, now: Optional[dt.datetime] = None, run_entry: bool = True, **kwargs) -> pd.DataFrame:
                if not (_is_entry_only_context() and _env_bool("SUMMARY_MAIN_EARLY_RETURN_AFTER_AI_SUBMIT", True)):
                    return orig_job_summary(interval, display=display, now=now, run_entry=run_entry, **kwargs)
                interval_i = int(interval)
                now_i = (now or rc.now_naive()).replace(microsecond=0)
                if interval_i != 1:
                    return orig_job_summary(interval_i, display=display, now=now_i, run_entry=run_entry, **kwargs)
                t0 = time.perf_counter()
                try:
                    runner = rc.resolve_push_summary_runner()
                    if not callable(runner):
                        raise RuntimeError("push summary runner is not available")
                    logger.warning(
                        "[SUMMARY MAIN LIGHT TICK] light job start interval=%s display=%s run_entry=%s now=%s extra_keys=%s",
                        interval_i,
                        display,
                        run_entry,
                        now_i,
                        sorted(list(kwargs.keys())),
                    )
                    result = rc.call_runner_with_optional_now(runner, interval=interval_i, now=now_i, **kwargs)
                    df, meta = rc.normalize_runner_output(result)
                    if not isinstance(df, pd.DataFrame) or df.empty:
                        logger.warning("[SUMMARY MAIN LIGHT TICK] light runner empty interval=%s -> fallback", interval_i)
                        df = rc.fallback_push_summary_df(interval_i, now=now_i)
                    df = _normalize_df_light(df, interval=interval_i, now=now_i)
                    if not df.empty:
                        # Keep global cache useful for other readers, but avoid display/enrich/Discord sync path.
                        try:
                            rc.log_job_result("job_summary(PUSH-LIGHT)", interval_i, df, meta if isinstance(meta, dict) else {})
                        except Exception:
                            pass
                        _submit_async_ai(df, interval=interval_i, now=now_i, run_entry=run_entry, reason="early_after_runner")
                    else:
                        logger.warning("[SUMMARY MAIN LIGHT TICK] light job empty after normalize interval=%s", interval_i)
                    logger.warning(
                        "[SUMMARY MAIN LIGHT TICK] light job return interval=%s rows=%s elapsed=%.3fs display_skipped=%s",
                        interval_i,
                        len(df) if isinstance(df, pd.DataFrame) else 0,
                        time.perf_counter() - t0,
                        not _env_bool("SUMMARY_MAIN_LIGHT_DISPLAY", False),
                    )
                    if _env_bool("SUMMARY_MAIN_LIGHT_DISPLAY", False):
                        try:
                            rc._display_push_sync_or_async(df, interval_i, now_i, display)
                        except Exception:
                            logger.exception("[SUMMARY MAIN LIGHT TICK] optional display submit failed interval=%s", interval_i)
                    return df if isinstance(df, pd.DataFrame) else pd.DataFrame()
                except Exception:
                    logger.exception("[SUMMARY MAIN LIGHT TICK] light job failed interval=%s -> original fallback", interval_i)
                    return orig_job_summary(interval_i, display=display, now=now_i, run_entry=run_entry, **kwargs)

            job_summary_light._main_light_tick_job_wrapped = True  # type: ignore[attr-defined]
            job_summary_light._original = orig_job_summary  # type: ignore[attr-defined]
            rc.job_summary = job_summary_light
            # Compatibility aliases used by other modules.
            try:
                rc.run_push_summary_job = lambda interval=1, display=True, now=None, run_entry=True, **kwargs: job_summary_light(int(interval), display=display, now=now, run_entry=run_entry, **kwargs)
                rc.job_1m = lambda display=True, now=None, run_entry=True: job_summary_light(1, display=display, now=now, run_entry=run_entry)
            except Exception:
                pass

        _INSTALLED = True
        logger.warning(
            "[SUMMARY MAIN LIGHT TICK] installed version=%s early_return=%s async_ai=%s skip_sync_save=%s main=%s",
            VERSION,
            os.getenv("SUMMARY_MAIN_EARLY_RETURN_AFTER_AI_SUBMIT"),
            os.getenv("SUMMARY_MAIN_ASYNC_AI_ENTRY"),
            os.getenv("SUMMARY_MAIN_SKIP_SYNC_SAVE"),
            _is_main_py(),
        )
        return True
    except Exception:
        logger.exception("[SUMMARY MAIN LIGHT TICK] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[SUMMARY MAIN LIGHT TICK] auto install failed")

__all__ = ["VERSION", "install"]
