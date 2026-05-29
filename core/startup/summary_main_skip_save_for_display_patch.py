# ============================================================
# File   : core/startup/summary_main_skip_save_for_display_patch.py
# Version: V2-MAIN-1M-DISPLAY-FIRST-ASYNC-DB-SAVE
# ------------------------------------------------------------
# 目的:
#   main.py(entry_only) 側で1分足PUSHサマリーを表示優先にしつつ、
#   DBにも保存する。
#
# 背景:
#   runner_core.job_summary() の順番は以下。
#     1. 計算
#     2. _save_summary_if_owner()
#     3. AI entry
#     4. display/Discord
#
#   main.py は entry_only なので cache_writer 側で
#     reason=main_entry_only
#   により DB保存がskipされる。
#   ただし、保存処理を完全に飛ばすと summary DB が更新されない。
#
# V2 方針:
#   - main.py / entry_only / 非database process の PUSH 1分足だけ、同期saveを実行しない
#   - 代わりに daemon Thread で direct SQLite 保存を実行する
#   - job_summary本体はすぐ次の AI/display へ進む
#   - main_database.py 側は従来通り通常保存
#
# ENV:
#   SUMMARY_MAIN_SKIP_1M_SAVE_BEFORE_DISPLAY=1  既定ON
#   SUMMARY_MAIN_SKIP_SAVE_BEFORE_DISPLAY_INTERVALS=1
#   SUMMARY_MAIN_ASYNC_DIRECT_DB_SAVE=1          既定ON
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import sqlite3
import threading
import time
from typing import Any, Optional

import pandas as pd

logger = logging.getLogger(__name__)

_PATCHED = False
_ORIGINAL_SAVE = None
_ASYNC_LOCK = threading.RLock()
_RUNNING_KEYS: set[str] = set()


def _env_flag(name: str) -> str:
    try:
        return str(os.getenv(name, "")).strip().lower()
    except Exception:
        return ""


def _env_bool(name: str, default: bool = True) -> bool:
    raw = _env_flag(name)
    if raw in {"1", "true", "yes", "y", "on", "enable", "enabled"}:
        return True
    if raw in {"0", "false", "no", "n", "off", "disable", "disabled"}:
        return False
    return bool(default)


def _is_database_process() -> bool:
    return any(
        _env_bool(name, False)
        for name in (
            "AUTOSTOCK_DATA_COLLECTORS_PROCESS",
            "AUTOSTOCK_SUMMARY_DB_WRITER",
            "AUTOSTOCK_MAIN_DATABASE_PROCESS",
        )
    )


def _is_main_entry_only() -> bool:
    role = _env_flag("SUMMARY_DB_WRITER_ROLE")
    return (
        _env_bool("SUMMARY_MAIN_ENTRY_ONLY", False)
        or _env_bool("SUMMARY_SKIP_DB_SAVE_IN_MAIN", False)
        or role == "entry_only"
    )


def _skip_intervals() -> set[int]:
    raw = os.getenv("SUMMARY_MAIN_SKIP_SAVE_BEFORE_DISPLAY_INTERVALS", "1")
    out: set[int] = set()
    for x in str(raw).replace(";", ",").split(","):
        x = x.strip()
        if not x:
            continue
        try:
            out.add(int(float(x)))
        except Exception:
            pass
    return out or {1}


def _should_display_first(interval: int, source: str) -> bool:
    if not _env_bool("SUMMARY_MAIN_SKIP_1M_SAVE_BEFORE_DISPLAY", True):
        return False
    if str(source).lower() not in {"push", "summary"}:
        return False
    if int(interval) not in _skip_intervals():
        return False
    if _is_database_process():
        return False
    if not _is_main_entry_only():
        return False
    return True


def _summary_table(interval: int) -> str:
    iv = int(interval)
    if iv == 1:
        return "stock_summary_1min"
    if iv == 3:
        return "stock_summary_3min"
    if iv == 5:
        return "stock_summary_5min"
    return f"stock_summary_{iv}min"


def _detect_yyyymmdd(df: pd.DataFrame) -> str:
    try:
        if "datetime" in df.columns:
            s = pd.to_datetime(df["datetime"], errors="coerce").dropna()
            if not s.empty:
                return pd.Timestamp(s.max()).strftime("%Y%m%d")
        if "date" in df.columns:
            s = pd.to_datetime(df["date"], errors="coerce").dropna()
            if not s.empty:
                return pd.Timestamp(s.max()).strftime("%Y%m%d")
    except Exception:
        pass
    return dt.datetime.now().strftime("%Y%m%d")


def _summary_db_path(df: pd.DataFrame) -> str:
    ymd = _detect_yyyymmdd(df)
    base = os.getenv(
        "SUMMARY_DB_DIR",
        r"\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\summary",
    )
    return os.path.join(base, f"summary{ymd}.db")


def _norm_df(df: Any) -> pd.DataFrame:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return pd.DataFrame()
    out = df.copy()
    if "symbol" in out.columns:
        out["symbol"] = out["symbol"].astype(str).str.replace(".0", "", regex=False).str.strip()
    if "datetime" in out.columns:
        out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce").dt.strftime("%Y-%m-%d %H:%M:%S")
    if "date" in out.columns:
        try:
            dts = pd.to_datetime(out["date"], errors="coerce")
            fmt = dts.dt.strftime("%Y-%m-%d")
            out["date"] = fmt.where(fmt.notna(), out["date"].astype(str))
        except Exception:
            out["date"] = out["date"].astype(str)
    if "time" in out.columns:
        out["time"] = out["time"].astype(str)
    if "time_range" in out.columns:
        out["time_range"] = out["time_range"].astype(str)
    if "source" not in out.columns:
        out["source"] = "push"
    else:
        out["source"] = out["source"].fillna("push").astype(str)
    if "last_update" not in out.columns:
        out["last_update"] = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return out


def _sqlite_value(v: Any):
    try:
        if pd.isna(v):
            return None
    except Exception:
        pass
    if isinstance(v, pd.Timestamp):
        return v.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(v, dt.datetime):
        return v.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(v, dt.date):
        return v.strftime("%Y-%m-%d")
    return v


def _key_cols(work: pd.DataFrame, table_cols: list[str], interval: int) -> list[str]:
    cols = set(work.columns)
    tcols = set(table_cols)
    if {"symbol", "datetime"}.issubset(cols) and {"symbol", "datetime"}.issubset(tcols):
        return ["symbol", "datetime"]
    if int(interval) in (3, 5) and {"symbol", "date", "time_range"}.issubset(cols) and {"symbol", "date", "time_range"}.issubset(tcols):
        return ["symbol", "date", "time_range"]
    if {"symbol", "date", "time"}.issubset(cols) and {"symbol", "date", "time"}.issubset(tcols):
        return ["symbol", "date", "time"]
    return []


def _direct_sqlite_save(df: pd.DataFrame, interval: int, source: str) -> int:
    work = _norm_df(df)
    if work.empty:
        return 0

    db_path = _summary_db_path(work)
    table = _summary_table(interval)

    con = None
    try:
        if not os.path.exists(db_path):
            logger.error("[SUMMARY MAIN ASYNC SAVE] db not found interval=%s table=%s path=%s", interval, table, db_path)
            return 0

        con = sqlite3.connect(db_path, timeout=10.0)
        con.execute("PRAGMA busy_timeout = 10000")
        try:
            con.execute("PRAGMA journal_mode = WAL")
            con.execute("PRAGMA synchronous = NORMAL")
        except Exception:
            pass

        exists = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        if exists is None:
            logger.error("[SUMMARY MAIN ASYNC SAVE] table not found interval=%s table=%s path=%s", interval, table, db_path)
            return 0

        table_cols = [str(r[1]) for r in con.execute(f"PRAGMA table_info({table})").fetchall()]
        keys = _key_cols(work, table_cols, interval)
        if not keys:
            logger.error("[SUMMARY MAIN ASYNC SAVE] no key columns interval=%s table=%s df_cols=%s table_cols=%s", interval, table, list(work.columns), table_cols)
            return 0

        if "source" in table_cols:
            work["source"] = str(source).lower()
        if "interval" in table_cols and "interval" not in work.columns:
            work["interval"] = int(interval)

        cols = [c for c in table_cols if c in work.columns and c != "id"]
        work = work.dropna(subset=keys).drop_duplicates(subset=keys, keep="last").reset_index(drop=True)
        if work.empty or not cols:
            return 0

        delete_sql = f"DELETE FROM {table} WHERE " + " AND ".join([f"{c}=?" for c in keys])
        delete_params = [tuple(_sqlite_value(row[c]) for c in keys) for _, row in work[keys].iterrows()]
        con.executemany(delete_sql, delete_params)

        insert_sql = f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join(['?'] * len(cols))})"
        records = [tuple(_sqlite_value(row.get(c)) for c in cols) for _, row in work[cols].iterrows()]
        con.executemany(insert_sql, records)
        con.commit()

        logger.warning(
            "[SUMMARY MAIN ASYNC SAVE] direct sqlite save done interval=%s source=%s rows=%s table=%s path=%s latest=%s",
            interval,
            source,
            len(records),
            table,
            db_path,
            work["datetime"].max() if "datetime" in work.columns else None,
        )
        return int(len(records))

    except sqlite3.OperationalError as e:
        logger.warning("[SUMMARY MAIN ASYNC SAVE] sqlite operational error interval=%s source=%s err=%s", interval, source, e)
        try:
            if con is not None:
                con.rollback()
        except Exception:
            pass
        return 0
    except Exception:
        logger.exception("[SUMMARY MAIN ASYNC SAVE] direct sqlite save failed interval=%s source=%s", interval, source)
        try:
            if con is not None:
                con.rollback()
        except Exception:
            pass
        return 0
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:
                pass


def _submit_async_save(df: pd.DataFrame, interval: int, source: str) -> None:
    if not _env_bool("SUMMARY_MAIN_ASYNC_DIRECT_DB_SAVE", True):
        return
    work = df.copy() if isinstance(df, pd.DataFrame) else pd.DataFrame()
    if work.empty:
        return
    latest = ""
    try:
        if "datetime" in work.columns:
            s = pd.to_datetime(work["datetime"], errors="coerce").dropna()
            if not s.empty:
                latest = pd.Timestamp(s.max()).strftime("%Y%m%d%H%M%S")
    except Exception:
        latest = ""
    key = f"{source}:{int(interval)}:{latest}:{len(work)}"
    with _ASYNC_LOCK:
        if key in _RUNNING_KEYS:
            logger.info("[SUMMARY MAIN ASYNC SAVE] duplicate skipped key=%s", key)
            return
        _RUNNING_KEYS.add(key)

    def _task() -> None:
        try:
            logger.warning("[SUMMARY MAIN ASYNC SAVE] start key=%s rows=%s", key, len(work))
            _direct_sqlite_save(work, int(interval), str(source))
        finally:
            with _ASYNC_LOCK:
                _RUNNING_KEYS.discard(key)

    th = threading.Thread(target=_task, name=f"summary-main-async-save-{key}", daemon=True)
    th.start()
    logger.warning("[SUMMARY MAIN ASYNC SAVE] submitted key=%s thread=%s", key, th.name)


def install() -> bool:
    global _PATCHED, _ORIGINAL_SAVE
    if _PATCHED:
        return True

    try:
        import scheduler_jobs.summary.runner_core as rc

        cur = getattr(rc, "_save_summary_if_owner", None)
        if not callable(cur):
            logger.warning("[SUMMARY MAIN DISPLAY-FIRST SAVE PATCH] target missing")
            return False
        if getattr(cur, "_summary_main_display_first_async_save_patch", False):
            _PATCHED = True
            return True

        _ORIGINAL_SAVE = cur

        def _patched_save_summary_if_owner(df, interval: int, *, source: str):
            try:
                iv = int(interval)
            except Exception:
                iv = interval

            if _should_display_first(int(iv), str(source)):
                try:
                    rows = len(df) if hasattr(df, "__len__") else 0
                except Exception:
                    rows = 0
                logger.warning(
                    "[SUMMARY MAIN DISPLAY-FIRST SAVE PATCH] async save then display interval=%s source=%s rows=%s role=%s main_entry_only=%s env_skip=%s",
                    iv,
                    source,
                    rows,
                    os.getenv("SUMMARY_DB_WRITER_ROLE", ""),
                    os.getenv("SUMMARY_MAIN_ENTRY_ONLY", ""),
                    os.getenv("SUMMARY_SKIP_DB_SAVE_IN_MAIN", ""),
                )
                _submit_async_save(df, int(iv), str(source))
                return None

            return _ORIGINAL_SAVE(df, int(iv), source=source)

        _patched_save_summary_if_owner._summary_main_display_first_async_save_patch = True  # type: ignore[attr-defined]
        _patched_save_summary_if_owner._summary_main_skip_save_display_patch = True  # type: ignore[attr-defined]
        _patched_save_summary_if_owner._original = cur  # type: ignore[attr-defined]
        rc._save_summary_if_owner = _patched_save_summary_if_owner
        _PATCHED = True
        logger.warning(
            "[SUMMARY MAIN DISPLAY-FIRST SAVE PATCH] installed enabled=%s intervals=%s async_direct=%s",
            _env_bool("SUMMARY_MAIN_SKIP_1M_SAVE_BEFORE_DISPLAY", True),
            sorted(_skip_intervals()),
            _env_bool("SUMMARY_MAIN_ASYNC_DIRECT_DB_SAVE", True),
        )
        return True

    except Exception:
        logger.exception("[SUMMARY MAIN DISPLAY-FIRST SAVE PATCH] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[SUMMARY MAIN DISPLAY-FIRST SAVE PATCH] auto install failed")


__all__ = ["install"]
