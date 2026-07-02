# -*- coding: utf-8 -*-
# ============================================================
# File   : core/startup/summary_main_push_db_refresh_patch.py
# Version: V2-MAIN-PUSH-DB-REFRESH-STALE-HISTORY-GUARD
# ------------------------------------------------------------
# main.py does not own the PUSH WebSocket/DB writer in split mode.
# It may start with a memory bootstrap snapshot, then the in-process
# memory can become stale while main_database.py keeps writing fresh
# pushYYYYMMDD.db rows. This patch refreshes the 1m-summary input from
# the PUSH DB when memory is stale, without enabling DB writes in main.py.
#
# V2:
#   - Guard GlobalContext push summary history/merged setters from stale
#     overwrite. If tf=1/source=push already has a newer latest_dt, an older
#     dataframe must not replace it. This prevents a fresh robust memory
#     summary such as 14:18 from being overwritten by stale 14:14 history.
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
VERSION = "V2-MAIN-PUSH-DB-REFRESH-STALE-HISTORY-GUARD"
_PATCHED = False
_CONTEXT_GUARD_PATCHED = False
_ORIG_LOAD_PUSH_MEMORY_DF = None
_ORIG_GC_SET_SUMMARY_HISTORY = None
_ORIG_GC_SET_MERGED_SUMMARY = None


def _env_bool(name: str, default: bool = True) -> bool:
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
        if v is not None and str(v).strip() != "":
            return float(str(v).replace(",", "").strip())
    except Exception:
        pass
    return float(default)


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is not None and str(v).strip() != "":
            return int(float(str(v).replace(",", "").strip()))
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


def _nas_root() -> str:
    for name in ("AUTOSTOCK_NAS_ROOT", "AUTO_STOCK_NAS_ROOT", "AUTOSTOCK_DATA_ROOT"):
        v = os.getenv(name)
        if v and str(v).strip():
            return str(v).strip()
    return r"\\192.168.0.22\AutoStockBuyAndSell"


def _today_yyyymmdd() -> str:
    return dt.datetime.now().strftime("%Y%m%d")


def _push_db_path() -> Path:
    explicit = os.getenv("PUSH_DB_PATH") or os.getenv("AUTOSTOCK_PUSH_DB_PATH")
    if explicit and str(explicit).strip():
        return Path(str(explicit).strip())
    return Path(_nas_root()) / "raw_data" / "kabu_station" / "push" / f"push{_today_yyyymmdd()}.db"


def _safe_dt_series(values: Any) -> pd.Series:
    try:
        s = values if isinstance(values, pd.Series) else pd.Series(values)
        out = pd.to_datetime(s, errors="coerce")
        try:
            if getattr(out.dt, "tz", None) is not None:
                out = out.dt.tz_convert("Asia/Tokyo").dt.tz_localize(None)
        except Exception:
            try:
                out = out.dt.tz_localize(None)
            except Exception:
                pass
        return out
    except Exception:
        return pd.Series(pd.NaT, index=getattr(values, "index", None))


def _latest_dt(df: Any) -> Any:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return None
    for c in ("datetime", "received_at", "time", "current_price_time", "PriceTime", "updated_at", "created_at"):
        if c in df.columns:
            try:
                x = _safe_dt_series(df[c]).max()
                if pd.notna(x):
                    return pd.Timestamp(x).tz_localize(None)
            except Exception:
                continue
    return None


def _stale_sec(df: pd.DataFrame) -> float | None:
    try:
        latest = _latest_dt(df)
        if latest is None or pd.isna(latest):
            return None
        return (pd.Timestamp(dt.datetime.now()).tz_localize(None) - pd.Timestamp(latest).tz_localize(None)).total_seconds()
    except Exception:
        return None


def _is_push_source(source: Any) -> bool:
    try:
        return str(source or "").strip().lower() in {"push", "push-cache", "push_summary", "push-summary"}
    except Exception:
        return False


def _is_guarded_tf(tf: Any) -> bool:
    try:
        s = str(tf).strip().lower().replace("min", "").replace("m", "")
        return s in {"1", "1.0"}
    except Exception:
        return False


def _latest_existing_from_store(store: Any, tf: Any, source: Any) -> tuple[Any, pd.DataFrame]:
    try:
        src = str(source or "legacy").lower()
        if not isinstance(store, dict):
            return None, pd.DataFrame()
        df = store.get(tf, {}).get(src)
        if not isinstance(df, pd.DataFrame) or df.empty:
            return None, pd.DataFrame()
        return _latest_dt(df), df.copy()
    except Exception:
        return None, pd.DataFrame()


def _should_block_stale_overwrite(*, kind: str, ctx: Any, tf: Any, source: Any, new_df: Any) -> tuple[bool, pd.DataFrame, Any, Any]:
    try:
        if not _env_bool("SUMMARY_PUSH_STALE_OVERWRITE_GUARD", True):
            return False, pd.DataFrame(), None, None
        if not (_is_guarded_tf(tf) and _is_push_source(source)):
            return False, pd.DataFrame(), None, None
        new_latest = _latest_dt(new_df)
        if new_latest is None or pd.isna(new_latest):
            return False, pd.DataFrame(), None, None
        store = getattr(ctx, "_summary_history", {}) if kind == "history" else getattr(ctx, "_merged_summary", {})
        old_latest, old_df = _latest_existing_from_store(store, tf, source)
        if old_latest is None or pd.isna(old_latest):
            return False, pd.DataFrame(), old_latest, new_latest
        max_back_sec = max(0.0, _env_float("SUMMARY_PUSH_STALE_OVERWRITE_MAX_BACK_SEC", 0.0))
        delta = (pd.Timestamp(old_latest).tz_localize(None) - pd.Timestamp(new_latest).tz_localize(None)).total_seconds()
        if delta > max_back_sec:
            return True, old_df, old_latest, new_latest
        return False, pd.DataFrame(), old_latest, new_latest
    except Exception:
        logger.debug("[SUMMARY PUSH STALE GUARD] compare failed kind=%s tf=%s source=%s", kind, tf, source, exc_info=True)
        return False, pd.DataFrame(), None, None


def _install_context_stale_guard() -> bool:
    global _CONTEXT_GUARD_PATCHED, _ORIG_GC_SET_SUMMARY_HISTORY, _ORIG_GC_SET_MERGED_SUMMARY
    if _CONTEXT_GUARD_PATCHED:
        return True
    if not _env_bool("SUMMARY_PUSH_STALE_OVERWRITE_GUARD", True):
        logger.warning("[SUMMARY PUSH STALE GUARD] disabled by env version=%s", VERSION)
        return False
    try:
        import core.global_context.context as ctx_mod

        cls = getattr(ctx_mod, "GlobalContext", None)
        if cls is None:
            logger.warning("[SUMMARY PUSH STALE GUARD] GlobalContext missing version=%s", VERSION)
            return False

        cur_hist = getattr(cls, "set_summary_history", None)
        cur_merged = getattr(cls, "set_merged_summary", None)
        if not callable(cur_hist) or not callable(cur_merged):
            logger.warning("[SUMMARY PUSH STALE GUARD] target setters missing version=%s", VERSION)
            return False

        if getattr(cur_hist, "_summary_push_stale_guard_v2", False) and getattr(cur_merged, "_summary_push_stale_guard_v2", False):
            _CONTEXT_GUARD_PATCHED = True
            return True

        _ORIG_GC_SET_SUMMARY_HISTORY = getattr(cur_hist, "_original", cur_hist)
        _ORIG_GC_SET_MERGED_SUMMARY = getattr(cur_merged, "_original", cur_merged)

        def _guarded_set_summary_history(self, tf: Any, df: Any, source: str = "legacy", caller: Any = None):
            block, old_df, old_latest, new_latest = _should_block_stale_overwrite(kind="history", ctx=self, tf=tf, source=source, new_df=df)
            if block:
                logger.warning(
                    "[SUMMARY PUSH STALE GUARD] blocked history stale overwrite tf=%s source=%s old_latest=%s new_latest=%s old_rows=%s new_rows=%s caller=%s version=%s",
                    tf,
                    source,
                    old_latest,
                    new_latest,
                    len(old_df) if isinstance(old_df, pd.DataFrame) else 0,
                    len(df) if isinstance(df, pd.DataFrame) else 0,
                    caller,
                    VERSION,
                )
                return old_df
            return _ORIG_GC_SET_SUMMARY_HISTORY(self, tf, df, source=source, caller=caller)

        def _guarded_set_merged_summary(self, tf: Any, df: Any, source: str = "legacy", caller: Any = None):
            block, old_df, old_latest, new_latest = _should_block_stale_overwrite(kind="merged", ctx=self, tf=tf, source=source, new_df=df)
            if block:
                logger.warning(
                    "[SUMMARY PUSH STALE GUARD] blocked merged stale overwrite tf=%s source=%s old_latest=%s new_latest=%s old_rows=%s new_rows=%s caller=%s version=%s",
                    tf,
                    source,
                    old_latest,
                    new_latest,
                    len(old_df) if isinstance(old_df, pd.DataFrame) else 0,
                    len(df) if isinstance(df, pd.DataFrame) else 0,
                    caller,
                    VERSION,
                )
                return old_df
            return _ORIG_GC_SET_MERGED_SUMMARY(self, tf, df, source=source, caller=caller)

        setattr(_guarded_set_summary_history, "_summary_push_stale_guard_v2", True)
        setattr(_guarded_set_summary_history, "_original", _ORIG_GC_SET_SUMMARY_HISTORY)
        setattr(_guarded_set_merged_summary, "_summary_push_stale_guard_v2", True)
        setattr(_guarded_set_merged_summary, "_original", _ORIG_GC_SET_MERGED_SUMMARY)
        cls.set_summary_history = _guarded_set_summary_history
        cls.set_merged_summary = _guarded_set_merged_summary
        _CONTEXT_GUARD_PATCHED = True
        logger.warning(
            "[SUMMARY PUSH STALE GUARD] installed max_back_sec=%.1f version=%s",
            _env_float("SUMMARY_PUSH_STALE_OVERWRITE_MAX_BACK_SEC", 0.0),
            VERSION,
        )
        return True
    except Exception:
        logger.exception("[SUMMARY PUSH STALE GUARD] install failed version=%s", VERSION)
        return False


def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    try:
        rows = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
        return [str(r[1]) for r in rows]
    except Exception:
        return []


def _existing_tables(conn: sqlite3.Connection) -> list[str]:
    try:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        return [str(r[0]) for r in rows]
    except Exception:
        return []


def _quote(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _read_push_db_recent() -> pd.DataFrame:
    if not _env_bool("SUMMARY_MAIN_PUSH_DB_REFRESH_ENABLED", True):
        return pd.DataFrame()
    path = _push_db_path()
    if not path.exists():
        logger.warning("[SUMMARY MAIN PUSH DB REFRESH] db missing path=%s version=%s", path, VERSION)
        return pd.DataFrame()

    limit = max(100, _env_int("SUMMARY_MAIN_PUSH_DB_REFRESH_LIMIT", 5000))
    timeout = max(0.05, _env_float("SUMMARY_MAIN_PUSH_DB_REFRESH_TIMEOUT_SEC", 0.8))
    tables_prefer = ["stream_data_raw", "stream_data", "push_stream", "push_ticks", "ticks"]
    try:
        conn = sqlite3.connect(str(path), timeout=timeout)
        try:
            conn.execute("PRAGMA query_only=ON")
            conn.execute("PRAGMA busy_timeout=%d" % int(timeout * 1000))
        except Exception:
            pass
        tables = _existing_tables(conn)
        for table in tables_prefer + [t for t in tables if t not in tables_prefer]:
            if table not in tables:
                continue
            cols = _table_columns(conn, table)
            if not cols:
                continue
            dt_col = next((c for c in ("datetime", "received_at", "created_at", "updated_at", "time") if c in cols), None)
            sym_col = next((c for c in ("symbol", "code", "Symbol", "銘柄コード") if c in cols), None)
            price_col = next((c for c in ("current_price", "price", "close", "CurrentPrice", "Price", "現在値") if c in cols), None)
            if sym_col is None or price_col is None:
                continue
            order = f"ORDER BY {_quote(dt_col)} DESC" if dt_col else ""
            sql = f"SELECT * FROM {_quote(table)} {order} LIMIT {int(limit)}"
            try:
                df = pd.read_sql_query(sql, conn)
            except Exception:
                continue
            if not isinstance(df, pd.DataFrame) or df.empty:
                continue
            latest = _latest_dt(df)
            logger.warning(
                "[SUMMARY MAIN PUSH DB REFRESH] loaded table=%s rows=%s latest=%s path=%s version=%s",
                table, len(df), latest, path, VERSION,
            )
            return df
        logger.warning("[SUMMARY MAIN PUSH DB REFRESH] no usable table path=%s tables=%s version=%s", path, tables[:20], VERSION)
        return pd.DataFrame()
    except Exception:
        logger.warning("[SUMMARY MAIN PUSH DB REFRESH] db read failed path=%s version=%s", path, VERSION, exc_info=True)
        return pd.DataFrame()
    finally:
        try:
            conn.close()  # type: ignore[name-defined]
        except Exception:
            pass


def _patched_load_push_memory_df() -> pd.DataFrame:
    base = pd.DataFrame()
    try:
        base = _ORIG_LOAD_PUSH_MEMORY_DF()
    except Exception:
        logger.warning("[SUMMARY MAIN PUSH DB REFRESH] original memory loader failed version=%s", VERSION, exc_info=True)

    if not _is_main_py():
        return base

    max_stale = max(10.0, _env_float("SUMMARY_MAIN_PUSH_DB_REFRESH_MAX_STALE_SEC", 75.0))
    stale = _stale_sec(base)
    if isinstance(base, pd.DataFrame) and not base.empty and stale is not None and stale <= max_stale:
        return base

    db_df = _read_push_db_recent()
    if db_df.empty:
        logger.warning(
            "[SUMMARY MAIN PUSH DB REFRESH] keep memory because db empty memory_rows=%s memory_stale=%s max_stale=%.1f version=%s",
            len(base) if isinstance(base, pd.DataFrame) else 0,
            None if stale is None else round(float(stale), 1),
            max_stale,
            VERSION,
        )
        return base

    db_stale = _stale_sec(db_df)
    logger.warning(
        "[SUMMARY MAIN PUSH DB REFRESH] using db rows=%s db_stale=%s memory_rows=%s memory_stale=%s max_stale=%.1f version=%s",
        len(db_df),
        None if db_stale is None else round(float(db_stale), 1),
        len(base) if isinstance(base, pd.DataFrame) else 0,
        None if stale is None else round(float(stale), 1),
        max_stale,
        VERSION,
    )
    return db_df


def install() -> bool:
    global _PATCHED, _ORIG_LOAD_PUSH_MEMORY_DF
    context_guard_ok = _install_context_stale_guard()
    if _PATCHED:
        return True
    if os.getenv("DISABLE_SUMMARY_MAIN_PUSH_DB_REFRESH_PATCH", "").strip() == "1":
        logger.warning("[SUMMARY MAIN PUSH DB REFRESH] disabled by env version=%s", VERSION)
        return False
    try:
        import core.startup.summary_main_memory_latest_1m_patch as target
        cur = getattr(target, "_load_push_memory_df", None)
        if not callable(cur):
            logger.warning("[SUMMARY MAIN PUSH DB REFRESH] target loader unavailable context_guard=%s version=%s", context_guard_ok, VERSION)
            return False
        if getattr(cur, "_summary_main_push_db_refresh_v2", False) or getattr(cur, "_summary_main_push_db_refresh_v1", False):
            _PATCHED = True
            return True
        _ORIG_LOAD_PUSH_MEMORY_DF = cur
        _patched_load_push_memory_df._summary_main_push_db_refresh_v2 = True  # type: ignore[attr-defined]
        _patched_load_push_memory_df._summary_main_push_db_refresh_v1 = True  # type: ignore[attr-defined]
        _patched_load_push_memory_df._original = cur  # type: ignore[attr-defined]
        target._load_push_memory_df = _patched_load_push_memory_df
        _PATCHED = True
        logger.warning(
            "[SUMMARY MAIN PUSH DB REFRESH] installed enabled=%s max_stale=%.1f limit=%s context_guard=%s version=%s",
            _env_bool("SUMMARY_MAIN_PUSH_DB_REFRESH_ENABLED", True),
            _env_float("SUMMARY_MAIN_PUSH_DB_REFRESH_MAX_STALE_SEC", 75.0),
            _env_int("SUMMARY_MAIN_PUSH_DB_REFRESH_LIMIT", 5000),
            context_guard_ok,
            VERSION,
        )
        return True
    except Exception:
        logger.exception("[SUMMARY MAIN PUSH DB REFRESH] install failed version=%s", VERSION)
        return False


try:
    install()
except Exception:
    logger.exception("[SUMMARY MAIN PUSH DB REFRESH] auto install failed version=%s", VERSION)


__all__ = ["install", "VERSION"]
