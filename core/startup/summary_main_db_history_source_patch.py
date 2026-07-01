# -*- coding: utf-8 -*-
# ============================================================
# File   : core/startup/summary_main_db_history_source_patch.py
# Version: V1-MAIN-1M-DB-HISTORY-SOURCE
# ------------------------------------------------------------
# main.py lightweight 1m summary patch.
#
# main.py must not start heavy 3m/5m summary jobs, but it still needs enough
# 1m history for MA/MACD/RSI. This patch supplements the PUSH summary source
# with recent rows from summaryYYYYMMDD.db, which is written by main_database.py.
#
# This patch is read-only and is disabled for main_database.py.
# ============================================================
from __future__ import annotations

import datetime as dt
import logging
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)
VERSION = "V1-MAIN-1M-DB-HISTORY-SOURCE"
_INSTALLED = False


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


def _is_main_py() -> bool:
    try:
        argv = " ".join(str(x).replace("\\", "/").lower() for x in (sys.argv or []))
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


def _normalize_history(df: pd.DataFrame, *, interval: int = 1) -> pd.DataFrame:
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


def _push_symbols(limit: int = 160) -> list[str]:
    gd = _global_data()
    if gd is None:
        return []
    try:
        candidates: list[Any] = []
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
        logger.debug("[SUMMARY MAIN DB HISTORY] push symbol detection failed", exc_info=True)
    return []


def _summary_db_candidates(trade_date: dt.date) -> list[str]:
    ymd = trade_date.strftime("%Y%m%d")
    explicit = [
        os.getenv("SUMMARY_MAIN_HISTORY_DB_PATH"),
        os.getenv("SUMMARY_DB_PATH"),
        os.getenv("SUMMARY_DB_FILE"),
    ]
    dirs = [
        os.getenv("SUMMARY_MAIN_HISTORY_DB_DIR"),
        os.getenv("SUMMARY_DB_DIR"),
        os.getenv("SUMMARY_DB_BASE_DIR"),
        os.getenv("AUTOSTOCK_SUMMARY_DIR"),
        os.getenv("KABU_SUMMARY_DIR"),
        r"\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\summary",
        r"\\192.168.0.22\AutoStockBuyAndSell\raw_data\summary",
        r"\\192.168.0.22\AutoStockBuyAndSell\summary",
    ]
    out: list[str] = []
    for p in explicit:
        if p and "YYYYMMDD" in p:
            out.append(str(p).replace("YYYYMMDD", ymd))
        elif p:
            out.append(str(p))
    for d in dirs:
        if d:
            out.append(str(Path(str(d)) / f"summary{ymd}.db"))
    seen = set()
    uniq = []
    for p in out:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq


def _read_db_history(interval: int = 1) -> pd.DataFrame:
    if int(interval) != 1:
        return pd.DataFrame()
    if not (_is_entry_only_context() and _env_bool("SUMMARY_MAIN_LOAD_DB_HISTORY", True)):
        return pd.DataFrame()
    trade_date = dt.datetime.now().date()
    table = os.getenv("SUMMARY_MAIN_HISTORY_TABLE") or f"stock_summary_{int(interval)}min"
    bars = max(5, _env_int("SUMMARY_MAIN_HISTORY_BARS", 90))
    lookback_min = max(10, _env_int("SUMMARY_MAIN_HISTORY_LOOKBACK_MIN", 180))
    max_symbols = max(1, _env_int("SUMMARY_MAIN_HISTORY_MAX_SYMBOLS", 160))
    symbols = _push_symbols(limit=max_symbols)
    since = (dt.datetime.now() - dt.timedelta(minutes=lookback_min)).strftime("%Y-%m-%d %H:%M:%S")

    for db_path in _summary_db_candidates(trade_date):
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
                    sql = f"SELECT * FROM {table} WHERE datetime >= ? ORDER BY datetime DESC LIMIT ?"
                    df = pd.read_sql_query(sql, con, params=[since, max_symbols * bars])
            df = _normalize_history(df, interval=interval)
            if df.empty:
                continue
            df = df.sort_values(["symbol", "datetime"], kind="stable")
            df = df.groupby("symbol", as_index=False, group_keys=False).tail(bars).reset_index(drop=True)
            logger.warning(
                "[SUMMARY MAIN DB HISTORY] loaded interval=%s rows=%s symbols=%s bars=%s lookback_min=%s db=%s",
                interval,
                len(df),
                int(df["symbol"].nunique()) if "symbol" in df.columns else 0,
                bars,
                lookback_min,
                db_path,
            )
            return df
        except Exception as e:
            logger.debug("[SUMMARY MAIN DB HISTORY] db candidate failed path=%s err=%s", db_path, e, exc_info=True)
    logger.warning(
        "[SUMMARY MAIN DB HISTORY] no db history loaded interval=%s candidates=%s symbols=%s",
        interval,
        len(_summary_db_candidates(trade_date)),
        len(symbols),
    )
    return pd.DataFrame()


def _merge_history(base: pd.DataFrame, db_hist: pd.DataFrame, *, interval: int = 1) -> pd.DataFrame:
    frames = []
    for x in (base, db_hist):
        df = _normalize_history(x, interval=interval)
        if not df.empty:
            frames.append(df)
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True, sort=False)
    out = out.loc[:, ~pd.Index(out.columns).duplicated()].copy()
    out = out.sort_values(["symbol", "datetime"], kind="stable")
    out = out.drop_duplicates(subset=["symbol", "datetime"], keep="last")
    bars = max(5, _env_int("SUMMARY_MAIN_HISTORY_BARS", 90))
    out = out.groupby("symbol", as_index=False, group_keys=False).tail(bars)
    return out.reset_index(drop=True)


def _base_history_is_immature(df: pd.DataFrame) -> bool:
    base = _normalize_history(df, interval=1)
    if base.empty:
        return True
    try:
        symbols = int(base["symbol"].nunique()) if "symbol" in base.columns else 0
        if len(base) <= max(5, symbols + 2):
            return True
        if "symbol_hist_len" in base.columns:
            h = pd.to_numeric(base["symbol_hist_len"], errors="coerce")
            if h.notna().any() and float(h.max()) <= 3.0:
                return True
    except Exception:
        return True
    return False


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    if not (_is_entry_only_context() and _env_bool("SUMMARY_MAIN_LOAD_DB_HISTORY", True)):
        logger.warning("[SUMMARY MAIN DB HISTORY] skipped context main=%s enabled=%s", _is_main_py(), os.getenv("SUMMARY_MAIN_LOAD_DB_HISTORY"))
        return False
    try:
        import trading.summary.engine.push_summary_engine as pse
        orig = getattr(pse, "_resolve_summary_source_df", None)
        if not callable(orig):
            logger.warning("[SUMMARY MAIN DB HISTORY] target missing")
            return False
        if getattr(orig, "_summary_main_db_history_wrapped", False):
            _INSTALLED = True
            return True

        def _patched_resolve_summary_source_df(interval: int) -> pd.DataFrame:
            base = orig(interval)
            try:
                if int(interval) != 1:
                    return base
                if (not _base_history_is_immature(base)) and not _env_bool("SUMMARY_MAIN_ALWAYS_MERGE_DB_HISTORY", False):
                    return base
                db_hist = _read_db_history(interval=1)
                merged = _merge_history(base, db_hist, interval=1)
                if not merged.empty:
                    logger.warning(
                        "[SUMMARY MAIN DB HISTORY] patched source interval=1 base_rows=%s db_rows=%s merged_rows=%s symbols=%s latest_dt=%s",
                        len(base) if isinstance(base, pd.DataFrame) else 0,
                        len(db_hist),
                        len(merged),
                        int(merged["symbol"].nunique()) if "symbol" in merged.columns else 0,
                        merged["datetime"].max() if "datetime" in merged.columns else None,
                    )
                    return merged
            except Exception:
                logger.exception("[SUMMARY MAIN DB HISTORY] patched resolve failed interval=%s", interval)
            return base

        _patched_resolve_summary_source_df._summary_main_db_history_wrapped = True  # type: ignore[attr-defined]
        _patched_resolve_summary_source_df._original = orig  # type: ignore[attr-defined]
        pse._resolve_summary_source_df = _patched_resolve_summary_source_df
        os.environ.setdefault("SUMMARY_MAIN_HISTORY_BARS", "90")
        os.environ.setdefault("SUMMARY_MAIN_HISTORY_LOOKBACK_MIN", "180")
        os.environ.setdefault("SUMMARY_MAIN_HISTORY_MAX_SYMBOLS", "160")
        _INSTALLED = True
        logger.warning(
            "[SUMMARY MAIN DB HISTORY] installed version=%s bars=%s lookback_min=%s",
            VERSION,
            os.getenv("SUMMARY_MAIN_HISTORY_BARS"),
            os.getenv("SUMMARY_MAIN_HISTORY_LOOKBACK_MIN"),
        )
        return True
    except Exception:
        logger.exception("[SUMMARY MAIN DB HISTORY] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[SUMMARY MAIN DB HISTORY] auto install failed")

__all__ = ["VERSION", "install"]
