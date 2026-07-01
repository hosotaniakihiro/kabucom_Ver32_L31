# -*- coding: utf-8 -*-
# ============================================================
# File   : core/startup/summary_main_1m_light_tick_patch.py
# Version: V5-MAIN-1M-LIGHT-AI-SCORED-FRESH-GUARD
# ------------------------------------------------------------
# main.py is entry-only: do not wait for/save/display heavy summary work.
# It only calculates PUSH 1m quickly, submits Summary-AI asynchronously, reads
# recent 1m history from summaryYYYYMMDD.db, and blocks slow raw push DB fallback
# from main.py 1m fallback path.
#
# V5:
#   - Summary AI async submit直前で入力DFを検査する。
#   - score_buy/score_sell/score_total/final_score/display_score が全て0のDFは
#     AIへ渡さず、GlobalContext上の scored completed 1m PUSH summary に差し替える。
#   - scored summary が無い/古すぎる場合は no_candidates を増やさないためAIをスキップ。
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
VERSION = "V5-MAIN-1M-LIGHT-AI-SCORED-FRESH-GUARD"
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


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is not None and str(v).strip() != "":
            return float(str(v).strip())
    except Exception:
        pass
    return float(default)


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
        out = out.loc[:, ~pd.Index(out.columns).duplicated()].copy()
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


def _num_series(df: pd.DataFrame, names: tuple[str, ...], default: float = 0.0) -> pd.Series:
    for name in names:
        try:
            if name in df.columns:
                return pd.to_numeric(df[name], errors="coerce").fillna(default).astype(float)
        except Exception:
            continue
    return pd.Series(default, index=df.index, dtype="float64")


def _score_profile(df: Any) -> dict[str, Any]:
    try:
        if not isinstance(df, pd.DataFrame) or df.empty:
            return {"rows": 0, "score_nonzero": 0, "buy_pos": 0, "sell_pos": 0, "latest": None, "age": None}
        buy = _num_series(df, ("ai_disp_buy_score", "config_buy_score", "score_buy", "buy_score", "buy", "buy_signal_score"), 0.0)
        sell = _num_series(df, ("ai_disp_sell_score", "config_sell_score", "score_sell", "sell_score", "sell", "sell_signal_score"), 0.0).abs()
        total = _num_series(df, ("ai_disp_total_score", "score_total", "total_score", "combined_score", "final_score", "display_score", "score"), 0.0)
        latest = None
        age = None
        if "datetime" in df.columns:
            try:
                dts = pd.to_datetime(df["datetime"], errors="coerce")
                try:
                    dts = dts.dt.tz_localize(None)
                except Exception:
                    pass
                latest_ts = dts.max()
                if pd.notna(latest_ts):
                    latest = latest_ts.to_pydatetime() if hasattr(latest_ts, "to_pydatetime") else latest_ts
                    age = (dt.datetime.now().replace(tzinfo=None) - latest.replace(tzinfo=None)).total_seconds()
            except Exception:
                pass
        return {
            "rows": len(df),
            "score_nonzero": int(((buy.abs() > 0) | (sell.abs() > 0) | (total.abs() > 0)).sum()),
            "buy_pos": int((buy > 0).sum()),
            "sell_pos": int((sell > 0).sum()),
            "total_pos": int((total > 0).sum()),
            "total_neg": int((total < 0).sum()),
            "buy_max": float(buy.max()) if len(buy) else 0.0,
            "sell_max": float(sell.max()) if len(sell) else 0.0,
            "latest": latest,
            "age": age,
        }
    except Exception:
        return {"rows": 0, "score_nonzero": 0, "buy_pos": 0, "sell_pos": 0, "latest": None, "age": None}


def _fresh_enough(df: pd.DataFrame) -> bool:
    prof = _score_profile(df)
    age = prof.get("age")
    if age is None:
        return True
    return float(age) <= _env_float("SUMMARY_MAIN_AI_MAX_SCORE_AGE_SEC", 300.0)


def _get_scored_context_summary(*, now: dt.datetime) -> pd.DataFrame:
    if not _env_bool("SUMMARY_MAIN_AI_USE_SCORED_CONTEXT", True):
        return pd.DataFrame()
    try:
        import core.global_context.context as ctx
        calls = (
            ("get_push_merged_summary", {"tf": 1}),
            ("get_push_merged_summary", {"interval": 1}),
            ("get_merged_summary", {"tf": 1, "source": "push"}),
            ("get_merged_summary", {"interval": 1, "source": "push"}),
            ("get_push_summary", {"tf": 1}),
            ("get_push_summary", {"interval": 1}),
        )
        best = pd.DataFrame()
        best_prof: dict[str, Any] = {"score_nonzero": -1, "age": None}
        best_name = ""
        for name, kwargs in calls:
            fn = getattr(ctx, name, None)
            if not callable(fn):
                continue
            try:
                df = fn(**kwargs)
            except TypeError:
                continue
            except Exception:
                continue
            x = _normalize_df_light(df, now=now) if isinstance(df, pd.DataFrame) else pd.DataFrame()
            if x.empty:
                continue
            prof = _score_profile(x)
            if int(prof.get("score_nonzero", 0) or 0) <= 0:
                continue
            if not _fresh_enough(x):
                logger.warning(
                    "[SUMMARY MAIN AI INPUT GUARD] skip stale scored context source=%s rows=%s latest=%s age=%s score_nonzero=%s",
                    name, prof.get("rows"), prof.get("latest"), prof.get("age"), prof.get("score_nonzero"),
                )
                continue
            if int(prof.get("score_nonzero", 0)) > int(best_prof.get("score_nonzero", -1)):
                best = x
                best_prof = prof
                best_name = name
        if isinstance(best, pd.DataFrame) and not best.empty:
            logger.warning(
                "[SUMMARY MAIN AI INPUT GUARD] selected scored context source=%s rows=%s latest=%s age=%s score_nonzero=%s buy_pos=%s sell_pos=%s",
                best_name, best_prof.get("rows"), best_prof.get("latest"), best_prof.get("age"), best_prof.get("score_nonzero"), best_prof.get("buy_pos"), best_prof.get("sell_pos"),
            )
            return best
    except Exception:
        logger.exception("[SUMMARY MAIN AI INPUT GUARD] scored context lookup failed")
    return pd.DataFrame()


def _prepare_ai_submit_df(df: pd.DataFrame, *, interval: int, now: dt.datetime, reason: str) -> pd.DataFrame:
    x = _normalize_df_light(df, now=now)
    if x.empty:
        return x
    if int(interval) != 1 or not _env_bool("SUMMARY_MAIN_AI_REQUIRE_SCORED_INPUT", True):
        return x
    prof = _score_profile(x)
    has_score = int(prof.get("score_nonzero", 0) or 0) > 0
    is_fresh = _fresh_enough(x)
    if has_score and is_fresh:
        logger.warning(
            "[SUMMARY MAIN AI INPUT GUARD] using current scored df rows=%s latest=%s age=%s score_nonzero=%s buy_pos=%s sell_pos=%s reason=%s",
            prof.get("rows"), prof.get("latest"), prof.get("age"), prof.get("score_nonzero"), prof.get("buy_pos"), prof.get("sell_pos"), reason,
        )
        return x
    repl = _get_scored_context_summary(now=now)
    if isinstance(repl, pd.DataFrame) and not repl.empty:
        rprof = _score_profile(repl)
        logger.warning(
            "[SUMMARY MAIN AI INPUT GUARD] replaced AI input current_rows=%s current_score_nonzero=%s current_latest=%s current_age=%s replacement_rows=%s replacement_score_nonzero=%s replacement_latest=%s replacement_age=%s reason=%s",
            prof.get("rows"), prof.get("score_nonzero"), prof.get("latest"), prof.get("age"),
            rprof.get("rows"), rprof.get("score_nonzero"), rprof.get("latest"), rprof.get("age"), reason,
        )
        return repl
    logger.warning(
        "[SUMMARY MAIN AI INPUT GUARD] async AI skipped reason=no_fresh_scored_df current_rows=%s current_score_nonzero=%s latest=%s age=%s submit_reason=%s",
        prof.get("rows"), prof.get("score_nonzero"), prof.get("latest"), prof.get("age"), reason,
    )
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
            seen.add(p)
            uniq.append(p)
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


def _install_no_raw_db_watchdog() -> bool:
    try:
        from core.startup.summary_main_no_raw_db_fallback_watchdog_patch import install as _install
        ok = bool(_install())
        logger.warning("[SUMMARY MAIN LIGHT TICK] no raw DB fallback watchdog install ok=%s", ok)
        return ok
    except Exception:
        logger.exception("[SUMMARY MAIN LIGHT TICK] no raw DB fallback watchdog install failed")
        return False


def _submit_async_ai(df: pd.DataFrame, *, interval: int, now: dt.datetime, run_entry: bool, reason: str) -> None:
    if not (_is_entry_only_context() and _env_bool("SUMMARY_MAIN_ASYNC_AI_ENTRY", True) and run_entry and int(interval) in (1, 3, 5)):
        return
    now_i = (now or dt.datetime.now()).replace(microsecond=0) if isinstance(now, dt.datetime) else dt.datetime.now().replace(microsecond=0)
    df_ai = _prepare_ai_submit_df(df, interval=int(interval), now=now_i, reason=reason)
    rows = len(df_ai) if isinstance(df_ai, pd.DataFrame) else 0
    if rows <= 0:
        logger.warning("[SUMMARY MAIN LIGHT TICK] async AI not submitted interval=%s reason=%s rows=0", interval, reason)
        return
    key = f"summary-ai:{int(interval)}:{now_i.strftime('%Y%m%d%H%M%S')}"
    with _AI_LOCK:
        if key in _AI_RUNNING_KEYS:
            logger.warning("[SUMMARY MAIN LIGHT TICK] async AI skipped already_running key=%s rows=%s", key, rows)
            return
        _AI_RUNNING_KEYS.add(key)
    df_copy = df_ai.copy(deep=False)

    def _task() -> None:
        try:
            prof = _score_profile(df_copy)
            logger.warning(
                "[SUMMARY MAIN LIGHT TICK] async AI start key=%s interval=%s rows=%s reason=%s latest=%s score_nonzero=%s buy_pos=%s sell_pos=%s",
                key, interval, rows, reason, prof.get("latest"), prof.get("score_nonzero"), prof.get("buy_pos"), prof.get("sell_pos"),
            )
            from scheduler_jobs.summary.summary_ai_entry_hook_v20 import run_summary_ai_entry_safe
            run_summary_ai_entry_safe(interval=int(interval), now=now_i, df=df_copy, source="SUMMARY")
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
        os.environ.setdefault("SUMMARY_MAIN_DISABLE_RAW_DB_FALLBACK", "1")
        os.environ.setdefault("SUMMARY_MAIN_SKIP_PUSH_DB_FALLBACK", "1")
        os.environ.setdefault("SUMMARY_MAIN_AI_REQUIRE_SCORED_INPUT", "1")
        os.environ.setdefault("SUMMARY_MAIN_AI_USE_SCORED_CONTEXT", "1")
        os.environ.setdefault("SUMMARY_MAIN_AI_MAX_SCORE_AGE_SEC", "300")
        _install_history_patch()
        _install_no_raw_db_watchdog()

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
                    _install_no_raw_db_watchdog()
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
        logger.warning(
            "[SUMMARY MAIN LIGHT TICK] installed version=%s main=%s db_history=%s no_raw_db=%s ai_scored_guard=%s ai_max_age=%s",
            VERSION, _is_main_py(), os.getenv("SUMMARY_MAIN_LOAD_DB_HISTORY"), os.getenv("SUMMARY_MAIN_DISABLE_RAW_DB_FALLBACK"),
            os.getenv("SUMMARY_MAIN_AI_REQUIRE_SCORED_INPUT"), os.getenv("SUMMARY_MAIN_AI_MAX_SCORE_AGE_SEC"),
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
