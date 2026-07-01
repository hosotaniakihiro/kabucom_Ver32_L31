# -*- coding: utf-8 -*-
# ============================================================
# File   : core/startup/summary_main_1m_light_tick_patch.py
# Version: V3-MAIN-1M-LIGHT-DB-HISTORY-COMPACT
# ------------------------------------------------------------
# main.py is entry-only: do not wait for/save/display heavy summary work.
# It only calculates PUSH 1m quickly, submits Summary-AI asynchronously, and
# reads recent 1m history from summaryYYYYMMDD.db so indicators are not limited
# to 1-2 bars. main_database.py remains the DB writer.
# ============================================================
from __future__ import annotations

import datetime as dt
import logging
import os
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Optional

import pandas as pd

logger = logging.getLogger(__name__)
VERSION = "V3-MAIN-1M-LIGHT-DB-HISTORY-COMPACT"
_INSTALLED = False
_HISTORY_INSTALLED = False
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
    except Exception:
        pass
    return bool(default)


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is not None and str(v).strip() != "":
            return int(float(str(v).strip()))
    except Exception:
        pass
    return int(default)


def _argv_text() -> str:
    try:
        return " ".join(str(x).replace("\\", "/").lower() for x in (sys.argv or []))
    except Exception:
        return ""


def _is_main_py() -> bool:
    argv = _argv_text()
    if any(x in argv for x in ("main_database.py", "data_collectors_runner.py", "summary_database_runner.py", "push_receiver_runner.py")):
        return False
    return "main.py" in argv


def _is_entry_only_context() -> bool:
    try:
        role = str(os.getenv("SUMMARY_DB_WRITER_ROLE") or "").strip().lower()
        return _is_main_py() or _env_bool("SUMMARY_MAIN_ENTRY_ONLY", False) or role in {"entry_only", "main_entry_only", "read_only", "no_save"}
    except Exception:
        return _is_main_py()


def _executor() -> ThreadPoolExecutor:
    global _AI_EXECUTOR
    if _AI_EXECUTOR is None:
        _AI_EXECUTOR = ThreadPoolExecutor(
            max_workers=max(1, _env_int("SUMMARY_MAIN_ASYNC_AI_WORKERS", 1)),
            thread_name_prefix="summary-main-ai-async",
        )
    return _AI_EXECUTOR


def _normalize_dt(s: Any) -> Any:
    try:
        return pd.to_datetime(s, errors="coerce").dt.tz_localize(None)
    except Exception:
        try:
            return pd.to_datetime(s, errors="coerce", utc=True).dt.tz_convert(None)
        except Exception:
            return s


def _normalize_df_light(df: pd.DataFrame, *, now: dt.datetime) -> pd.DataFrame:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return pd.DataFrame()
    try:
        out = df.copy(deep=False)
        if "datetime" in out.columns:
            out["datetime"] = _normalize_dt(out["datetime"])
            cutoff = pd.Timestamp(now).tz_localize(None)
            out = out[out["datetime"].isna() | (out["datetime"] <= cutoff)]
        if "symbol" in out.columns:
            out["symbol"] = out["symbol"].astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
            out = out[out["symbol"].ne("")]
        return out.reset_index(drop=True)
    except Exception:
        logger.exception("[SUMMARY MAIN LIGHT TICK] normalize failed")
        return df if isinstance(df, pd.DataFrame) else pd.DataFrame()


def _global_data() -> Any:
    try:
        from global_state import global_data
        return global_data
    except Exception:
        try:
            from core.global_context.context import global_data  # type: ignore
            return global_data
        except Exception:
            return None


def _normalize_hist(df: pd.DataFrame, *, interval: int = 1) -> pd.DataFrame:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return pd.DataFrame()
    try:
        out = df.copy()
        out = out.loc[:, ~pd.Index(out.columns).duplicated()].copy()
        if "symbol" not in out.columns:
            for c in ("Symbol", "Code", "code", "symbol_code"):
                if c in out.columns:
                    out["symbol"] = out[c]
                    break
        if "datetime" not in out.columns:
            for c in ("Datetime", "date_time", "timestamp", "end_time", "start_time"):
                if c in out.columns:
                    out["datetime"] = out[c]
                    break
        if "symbol" not in out.columns or "datetime" not in out.columns:
            return pd.DataFrame()
        out["symbol"] = out["symbol"].astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
        out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
        try:
            out["datetime"] = out["datetime"].dt.tz_localize(None)
        except Exception:
            pass
        out = out.dropna(subset=["symbol", "datetime"])
        out = out[out["symbol"].ne("")]
        if out.empty:
            return pd.DataFrame()
        out["interval"] = int(interval)
        return out.reset_index(drop=True)
    except Exception:
        logger.exception("[SUMMARY MAIN DB HISTORY] normalize failed interval=%s", interval)
        return pd.DataFrame()


def _push_symbols(limit: int) -> list[str]:
    gd = _global_data()
    if gd is None:
        return []
    try:
        candidates = []
        for name in ("push_df", "stream_data", "latest_push_df", "push_data", "push_snapshot_df"):
            try:
                candidates.append(getattr(gd, name, None))
            except Exception:
                pass
        try:
            fn = getattr(gd, "get_push_df", None)
            if callable(fn):
                candidates.append(fn())
        except Exception:
            pass
        for x in candidates:
            if isinstance(x, pd.DataFrame) and not x.empty and "symbol" in x.columns:
                syms = (
                    x["symbol"].astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
                    .loc[lambda s: s.ne("")]
                    .drop_duplicates()
                    .head(int(limit))
                    .tolist()
                )
                if syms:
                    return syms
    except Exception:
        logger.debug("[SUMMARY MAIN DB HISTORY] push symbols failed", exc_info=True)
    return []


def _db_candidates(day: dt.date) -> list[str]:
    ymd = day.strftime("%Y%m%d")
    explicit = [os.getenv("SUMMARY_MAIN_HISTORY_DB_PATH"), os.getenv("SUMMARY_DB_PATH"), os.getenv("SUMMARY_DB_FILE")]
    dirs = [
        os.getenv("SUMMARY_MAIN_HISTORY_DB_DIR"), os.getenv("SUMMARY_DB_DIR"), os.getenv("SUMMARY_DB_BASE_DIR"),
        os.getenv("AUTOSTOCK_SUMMARY_DIR"), os.getenv("KABU_SUMMARY_DIR"),
        r"\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\summary",
        r"\\192.168.0.22\AutoStockBuyAndSell\raw_data\summary",
        r"\\192.168.0.22\AutoStockBuyAndSell\summary",
    ]
    out = []
    for p in explicit:
        if p and "YYYYMMDD" in p:
            out.append(str(p).replace("YYYYMMDD", ymd))
        elif p:
            out.append(str(p))
    for d in dirs:
        if d:
            out.append(str(Path(str(d)) / f"summary{ymd}.db"))
    seen, uniq = set(), []
    for p in out:
        if p not in seen:
            seen.add(p); uniq.append(p)
    return uniq


def _read_db_history(interval: int = 1) -> pd.DataFrame:
    if int(interval) != 1 or not _env_bool("SUMMARY_MAIN_LOAD_DB_HISTORY", True):
        return pd.DataFrame()
    bars = max(5, _env_int("SUMMARY_MAIN_HISTORY_BARS", 90))
    lookback_min = max(10, _env_int("SUMMARY_MAIN_HISTORY_LOOKBACK_MIN", 180))
    max_symbols = max(1, _env_int("SUMMARY_MAIN_HISTORY_MAX_SYMBOLS", 160))
    table = os.getenv("SUMMARY_MAIN_HISTORY_TABLE") or f"stock_summary_{int(interval)}min"
    symbols = _push_symbols(max_symbols)
    since = (dt.datetime.now() - dt.timedelta(minutes=lookback_min)).strftime("%Y-%m-%d %H:%M:%S")
    for db_path in _db_candidates(dt.datetime.now().date()):
        try:
            if not db_path or not os.path.exists(db_path):
                continue
            with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1.0) as con:
                if symbols:
                    parts = []
                    for i in range(0, len(symbols), 80):
                        chunk = symbols[i:i + 80]
                        ph = ",".join(["?"] * len(chunk))
                        sql = f"SELECT * FROM {table} WHERE datetime >= ? AND symbol IN ({ph}) ORDER BY symbol, datetime"
                        parts.append(pd.read_sql_query(sql, con, params=[since] + chunk))
                    df = pd.concat(parts, ignore_index=True, sort=False) if parts else pd.DataFrame()
                else:
                    df = pd.read_sql_query(f"SELECT * FROM {table} WHERE datetime >= ? ORDER BY datetime DESC LIMIT ?", con, params=[since, max_symbols * bars])
            df = _normalize_hist(df, interval=interval)
            if df.empty:
                continue
            df = df.sort_values(["symbol", "datetime"], kind="stable")
            df = df.groupby("symbol", as_index=False, group_keys=False).tail(bars).reset_index(drop=True)
            logger.warning("[SUMMARY MAIN DB HISTORY] loaded interval=%s rows=%s symbols=%s db=%s", interval, len(df), int(df["symbol"].nunique()), db_path)
            return df
        except Exception as e:
            logger.debug("[SUMMARY MAIN DB HISTORY] db candidate failed path=%s err=%s", db_path, e, exc_info=True)
    logger.warning("[SUMMARY MAIN DB HISTORY] no db history loaded interval=%s symbols=%s", interval, len(symbols))
    return pd.DataFrame()


def _merge_hist(base: pd.DataFrame, db: pd.DataFrame, *, interval: int = 1) -> pd.DataFrame:
    frames = [x for x in (_normalize_hist(base, interval=interval), _normalize_hist(db, interval=interval)) if isinstance(x, pd.DataFrame) and not x.empty]
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True, sort=False)
    out = out.loc[:, ~pd.Index(out.columns).duplicated()].copy()
    out = out.sort_values(["symbol", "datetime"], kind="stable")
    out = out.drop_duplicates(subset=["symbol", "datetime"], keep="last")
    out = out.groupby("symbol", as_index=False, group_keys=False).tail(max(5, _env_int("SUMMARY_MAIN_HISTORY_BARS", 90)))
    return out.reset_index(drop=True)


def _hist_immature(df: pd.DataFrame) -> bool:
    x = _normalize_hist(df, interval=1)
    if x.empty:
        return True
    try:
        symbols = int(x["symbol"].nunique()) if "symbol" in x.columns else 0
        if len(x) <= max(5, symbols + 2):
            return True
        if "symbol_hist_len" in x.columns:
            h = pd.to_numeric(x["symbol_hist_len"], errors="coerce")
            if h.notna().any() and float(h.max()) <= 3.0:
                return True
    except Exception:
        return True
    return False


def _install_history_patch() -> bool:
    global _HISTORY_INSTALLED
    if _HISTORY_INSTALLED:
        return True
    try:
        import trading.summary.engine.push_summary_engine as pse
        orig = getattr(pse, "_resolve_summary_source_df", None)
        if not callable(orig):
            return False
        if getattr(orig, "_summary_main_db_history_wrapped", False):
            _HISTORY_INSTALLED = True
            return True

        def _patched(interval: int) -> pd.DataFrame:
            base = orig(interval)
            try:
                if int(interval) != 1:
                    return base
                if not _hist_immature(base) and not _env_bool("SUMMARY_MAIN_ALWAYS_MERGE_DB_HISTORY", False):
                    return base
                db = _read_db_history(interval=1)
                merged = _merge_hist(base, db, interval=1)
                if not merged.empty:
                    logger.warning(
                        "[SUMMARY MAIN DB HISTORY] patched source interval=1 base_rows=%s db_rows=%s merged_rows=%s symbols=%s latest_dt=%s",
                        len(base) if isinstance(base, pd.DataFrame) else 0, len(db), len(merged), int(merged["symbol"].nunique()), merged["datetime"].max(),
                    )
                    return merged
            except Exception:
                logger.exception("[SUMMARY MAIN DB HISTORY] patched resolve failed interval=%s", interval)
            return base

        _patched._summary_main_db_history_wrapped = True  # type: ignore[attr-defined]
        _patched._original = orig  # type: ignore[attr-defined]
        pse._resolve_summary_source_df = _patched
        _HISTORY_INSTALLED = True
        logger.warning("[SUMMARY MAIN DB HISTORY] installed bars=%s lookback=%s", os.getenv("SUMMARY_MAIN_HISTORY_BARS"), os.getenv("SUMMARY_MAIN_HISTORY_LOOKBACK_MIN"))
        return True
    except Exception:
        logger.exception("[SUMMARY MAIN DB HISTORY] install failed")
        return False


def _submit_async_ai(df: pd.DataFrame, *, interval: int, now: dt.datetime, run_entry: bool, reason: str) -> None:
    if not (_is_entry_only_context() and _env_bool("SUMMARY_MAIN_ASYNC_AI_ENTRY", True) and run_entry and int(interval) in (1, 3, 5)):
        return
    rows = len(df) if isinstance(df, pd.DataFrame) else 0
    if rows <= 0:
        return
    key = f"summary-ai:{int(interval)}:{now.strftime('%Y%m%d%H%M%S') if isinstance(now, dt.datetime) else now}"
    with _AI_LOCK:
        if key in _AI_RUNNING_KEYS:
            logger.warning("[SUMMARY MAIN LIGHT TICK] async AI skipped already_running key=%s rows=%s", key, rows)
            return
        _AI_RUNNING_KEYS.add(key)
    df_copy = df.copy(deep=False)

    def _task() -> None:
        try:
            logger.warning("[SUMMARY MAIN LIGHT TICK] async AI start key=%s interval=%s rows=%s reason=%s", key, interval, rows, reason)
            from scheduler_jobs.summary.summary_ai_entry_hook_v20 import run_summary_ai_entry_safe
            run_summary_ai_entry_safe(interval=int(interval), now=now, df=df_copy, source="SUMMARY")
        except Exception:
            logger.exception("[SUMMARY MAIN LIGHT TICK] async AI failed key=%s interval=%s", key, interval)
        finally:
            with _AI_LOCK:
                _AI_RUNNING_KEYS.discard(key)
            logger.warning("[SUMMARY MAIN LIGHT TICK] async AI done key=%s interval=%s", key, interval)

    _executor().submit(_task)
    logger.warning("[SUMMARY MAIN LIGHT TICK] async AI submitted key=%s interval=%s rows=%s reason=%s", key, interval, rows, reason)


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
        os.environ.setdefault("SUMMARY_MAIN_LOAD_DB_HISTORY", "1")
        os.environ.setdefault("SUMMARY_MAIN_HISTORY_BARS", "90")
        os.environ.setdefault("SUMMARY_MAIN_HISTORY_LOOKBACK_MIN", "180")
        os.environ.setdefault("SUMMARY_MAIN_HISTORY_MAX_SYMBOLS", "160")
        _install_history_patch()

        import scheduler_jobs.summary.runner_core as rc
        orig_job_summary = getattr(rc, "job_summary", None)
        orig_ai = getattr(rc, "_run_push_ai_entry_before_display", None)
        orig_save = getattr(rc, "_save_summary_if_owner", None)

        if callable(orig_save) and not getattr(orig_save, "_main_light_tick_wrapped", False):
            def _save_light(df: pd.DataFrame, interval: int, *, source: str) -> None:
                if _is_entry_only_context() and _env_bool("SUMMARY_MAIN_SKIP_SYNC_SAVE", True):
                    logger.warning("[SUMMARY MAIN LIGHT TICK] sync save skipped in main interval=%s source=%s rows=%s reason=database_owner_main_database", interval, source, len(df) if isinstance(df, pd.DataFrame) else 0)
                    return None
                return orig_save(df, interval, source=source)
            _save_light._main_light_tick_wrapped = True  # type: ignore[attr-defined]
            _save_light._original = orig_save  # type: ignore[attr-defined]
            rc._save_summary_if_owner = _save_light

        if callable(orig_ai) and not getattr(orig_ai, "_main_light_tick_wrapped", False):
            def _ai_light(df: pd.DataFrame, interval: int, now: dt.datetime, run_entry: bool) -> None:
                _submit_async_ai(df, interval=int(interval), now=now, run_entry=run_entry, reason="original_ai_hook_async")
                return None
            _ai_light._main_light_tick_wrapped = True  # type: ignore[attr-defined]
            _ai_light._original = orig_ai  # type: ignore[attr-defined]
            rc._run_push_ai_entry_before_display = _ai_light

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
                    _install_history_patch()
                    runner = rc.resolve_push_summary_runner()
                    if not callable(runner):
                        raise RuntimeError("push summary runner is not available")
                    logger.warning("[SUMMARY MAIN LIGHT TICK] light job start interval=%s display=%s run_entry=%s now=%s", interval_i, display, run_entry, now_i)
                    result = rc.call_runner_with_optional_now(runner, interval=interval_i, now=now_i, **kwargs)
                    df, meta = rc.normalize_runner_output(result)
                    if not isinstance(df, pd.DataFrame) or df.empty:
                        logger.warning("[SUMMARY MAIN LIGHT TICK] light runner empty interval=%s -> fallback", interval_i)
                        df = rc.fallback_push_summary_df(interval_i, now=now_i)
                    df = _normalize_df_light(df, now=now_i)
                    if not df.empty:
                        try:
                            rc.log_job_result("job_summary(PUSH-LIGHT)", interval_i, df, meta if isinstance(meta, dict) else {})
                        except Exception:
                            pass
                        _submit_async_ai(df, interval=interval_i, now=now_i, run_entry=run_entry, reason="early_after_runner")
                    logger.warning("[SUMMARY MAIN LIGHT TICK] light job return interval=%s rows=%s elapsed=%.3fs display_skipped=%s", interval_i, len(df) if isinstance(df, pd.DataFrame) else 0, time.perf_counter() - t0, not _env_bool("SUMMARY_MAIN_LIGHT_DISPLAY", False))
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
            rc.run_push_summary_job = lambda interval=1, display=True, now=None, run_entry=True, **kwargs: job_summary_light(int(interval), display=display, now=now, run_entry=run_entry, **kwargs)
            rc.job_1m = lambda display=True, now=None, run_entry=True: job_summary_light(1, display=display, now=now, run_entry=run_entry)

        _INSTALLED = True
        logger.warning("[SUMMARY MAIN LIGHT TICK] installed version=%s main=%s db_history=%s", VERSION, _is_main_py(), os.getenv("SUMMARY_MAIN_LOAD_DB_HISTORY"))
        return True
    except Exception:
        logger.exception("[SUMMARY MAIN LIGHT TICK] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[SUMMARY MAIN LIGHT TICK] auto install failed")

__all__ = ["VERSION", "install"]
