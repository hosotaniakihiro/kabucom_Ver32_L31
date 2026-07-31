# -*- coding: utf-8 -*-
# ============================================================
# File   : core/startup/summary_main_push_db_refresh_patch.py
# Version: V3-MAIN-PUSH-DB-REFRESH-STALE-GUARD-INLINED
# ------------------------------------------------------------
# main.py does not own the PUSH WebSocket/DB writer in split mode.
# It may start with a memory bootstrap snapshot, then the in-process
# memory can become stale while main_database.py keeps writing fresh
# pushYYYYMMDD.db rows. This patch refreshes the 1m-summary input from
# the PUSH DB when memory is stale, without enabling DB writes in main.py.
#
# V3:
#   - The V2 stale-overwrite guard (class-level monkeypatch of
#     GlobalContext.set_summary_history/set_merged_summary) is inlined into
#     core/global_context/context.py directly. It used to race against
#     core/startup/global_context_summary_repair_patch.py's instance-level
#     MethodType patch of the same singleton's set_merged_summary --
#     whichever installed second would win permanently (instance attributes
#     shadow class attributes in Python), so the guard could silently never
#     take effect depending on background-thread timing.
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
VERSION = "V3-MAIN-PUSH-DB-REFRESH-STALE-GUARD-INLINED"
_PATCHED = False
_CONTEXT_GUARD_PATCHED = False
_ORIG_LOAD_PUSH_MEMORY_DF = None


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


# set_summary_history/set_merged_summary の stale-overwrite guard
# (旧 _install_context_stale_guard / _guarded_set_summary_history /
# _guarded_set_merged_summary) は core/global_context/context.py 本体 (REV11)
# へインライン化済み。これにより、本パッチ (class属性へ差し替え) と
# core/startup/global_context_summary_repair_patch.py (instance属性へ差し替え)
# が競合し、起動順序次第でどちらかが恒久的にシャドウされる不具合も解消された。


def _install_context_stale_guard() -> bool:
    global _CONTEXT_GUARD_PATCHED
    _CONTEXT_GUARD_PATCHED = True
    return True


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
