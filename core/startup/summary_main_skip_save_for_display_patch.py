# ============================================================
# File   : core/startup/summary_main_skip_save_for_display_patch.py
# Version: V5-MAIN-DISPLAY-FIRST-SPOOL-NO-DIRECT-DEFAULT
# ------------------------------------------------------------
# 目的:
#   main.py 側で1分足PUSHサマリーを表示・AI優先にし、NAS SQLiteへの重い直接保存で
#   summary_parent_tick / entry / exit_loop を詰まらせない。
#
# V5 修正:
#   ✔ main.py側の direct SQLite 保存をデフォルトOFFに変更
#   ✔ main.pyはDBに直接BEGIN IMMEDIATEしない。必要時は jsonl.gz spool のみ
#   ✔ direct保存は SUMMARY_MAIN_ASYNC_DIRECT_DB_SAVE=1 の明示時だけ許可
#   ✔ 09:12データ保存に180秒級かかる症状を避ける
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import sqlite3
import sys
import threading
import time
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

_PATCHED = False
_ORIGINAL_SAVE = None
_ASYNC_LOCK = threading.RLock()
_RUNNING_KEYS: set[str] = set()
_LAST_DISABLED_LOG_TS = 0.0


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


def _env_float(name: str, default: float, *, min_value: float | None = None, max_value: float | None = None) -> float:
    try:
        v = float(str(os.getenv(name, str(default))).strip())
        if min_value is not None:
            v = max(v, min_value)
        if max_value is not None:
            v = min(v, max_value)
        return v
    except Exception:
        return float(default)


def _is_database_process() -> bool:
    return any(
        _env_bool(name, False)
        for name in (
            "AUTOSTOCK_DATA_COLLECTORS_PROCESS",
            "AUTOSTOCK_SUMMARY_DB_WRITER",
            "AUTOSTOCK_MAIN_DATABASE_PROCESS",
        )
    )


def _is_main_py_process() -> bool:
    try:
        argv = " ".join(str(x).replace("\\", "/").lower() for x in sys.argv)
        return "main.py" in argv and not _is_database_process()
    except Exception:
        return False


def _is_main_entry_or_full() -> bool:
    role = _env_flag("SUMMARY_DB_WRITER_ROLE")
    return (
        _is_main_py_process()
        or _env_bool("SUMMARY_MAIN_ENTRY_ONLY", False)
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
    if not _is_main_entry_or_full():
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


def _spool_on_failure(df: pd.DataFrame, *, interval: int, source: str, reason: str) -> str:
    if not _env_bool("SUMMARY_MAIN_ASYNC_SPOOL_ON_LOCK", True):
        return ""
    try:
        from trading.summary.persistence.summary_save_spool import spool_summary_df
        path = spool_summary_df(df, interval=int(interval), source=str(source), reason=reason)
        logger.warning(
            "[SUMMARY MAIN SPOOL] spooled interval=%s source=%s rows=%s reason=%s path=%s",
            interval,
            source,
            len(df) if hasattr(df, "__len__") else 0,
            reason,
            path,
        )
        return path
    except Exception:
        logger.exception("[SUMMARY MAIN SPOOL] spool failed interval=%s source=%s reason=%s", interval, source, reason)
        return ""


def _direct_sqlite_save(df: pd.DataFrame, interval: int, source: str) -> int:
    """明示 env 時だけ使う保険。通常main.pyでは使わない。"""
    t0 = time.perf_counter()
    work = _norm_df(df)
    if work.empty:
        logger.warning("[SUMMARY MAIN ASYNC SAVE] empty after normalize interval=%s source=%s", interval, source)
        return 0

    db_path = _summary_db_path(work)
    table = _summary_table(interval)
    connect_timeout = _env_float("SUMMARY_MAIN_ASYNC_SQLITE_TIMEOUT_SEC", 0.2, min_value=0.05, max_value=1.0)
    busy_timeout_ms = int(_env_float("SUMMARY_MAIN_ASYNC_SQLITE_BUSY_MS", 100.0, min_value=50.0, max_value=500.0))

    con = None
    try:
        logger.warning(
            "[SUMMARY MAIN ASYNC SAVE] direct start interval=%s source=%s rows=%s table=%s timeout=%.2fs busy_ms=%s latest=%s",
            interval,
            source,
            len(work),
            table,
            connect_timeout,
            busy_timeout_ms,
            work["datetime"].max() if "datetime" in work.columns else None,
        )

        if not os.path.exists(db_path):
            _spool_on_failure(work, interval=interval, source=source, reason="db_not_found")
            return 0

        con = sqlite3.connect(db_path, timeout=connect_timeout, isolation_level=None)
        con.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
        exists = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        if exists is None:
            _spool_on_failure(work, interval=interval, source=source, reason="table_not_found")
            return 0

        table_cols = [str(r[1]) for r in con.execute(f"PRAGMA table_info({table})").fetchall()]
        keys = _key_cols(work, table_cols, interval)
        if not keys:
            _spool_on_failure(work, interval=interval, source=source, reason="no_key_columns")
            return 0

        if "source" in table_cols:
            work["source"] = str(source).lower()
        if "interval" in table_cols and "interval" not in work.columns:
            work["interval"] = int(interval)

        cols = [c for c in table_cols if c in work.columns and c != "id"]
        work = work.dropna(subset=keys).drop_duplicates(subset=keys, keep="last").reset_index(drop=True)
        if work.empty or not cols:
            return 0

        con.execute("BEGIN IMMEDIATE")
        delete_sql = f"DELETE FROM {table} WHERE " + " AND ".join([f"{c}=?" for c in keys])
        delete_params = [tuple(_sqlite_value(row[c]) for c in keys) for _, row in work[keys].iterrows()]
        con.executemany(delete_sql, delete_params)

        insert_sql = f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join(['?'] * len(cols))})"
        records = [tuple(_sqlite_value(row.get(c)) for c in cols) for _, row in work[cols].iterrows()]
        con.executemany(insert_sql, records)
        con.commit()

        elapsed = time.perf_counter() - t0
        logger.warning(
            "[SUMMARY MAIN ASYNC SAVE] direct sqlite save done interval=%s source=%s rows=%s latest=%s elapsed=%.3fs",
            interval,
            source,
            len(records),
            work["datetime"].max() if "datetime" in work.columns else None,
            elapsed,
        )
        return int(len(records))

    except sqlite3.OperationalError as e:
        try:
            if con is not None:
                con.rollback()
        except Exception:
            pass
        logger.warning(
            "[SUMMARY MAIN ASYNC SAVE] sqlite operational error interval=%s source=%s err=%s elapsed=%.3fs action=spool_only",
            interval,
            source,
            e,
            time.perf_counter() - t0,
        )
        _spool_on_failure(work, interval=interval, source=source, reason="sqlite_operational_error")
        return 0
    except Exception:
        try:
            if con is not None:
                con.rollback()
        except Exception:
            pass
        logger.exception("[SUMMARY MAIN ASYNC SAVE] direct sqlite save failed interval=%s source=%s elapsed=%.3fs", interval, source, time.perf_counter() - t0)
        _spool_on_failure(work, interval=interval, source=source, reason="exception")
        return 0
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:
                pass


def _submit_async_save(df: pd.DataFrame, interval: int, source: str) -> None:
    work = df.copy() if isinstance(df, pd.DataFrame) else pd.DataFrame()
    if work.empty:
        logger.warning("[SUMMARY MAIN DISPLAY-FIRST SAVE PATCH] skipped empty df interval=%s source=%s", interval, source)
        return

    # 重要: main.py の direct SQLite save はデフォルトOFF。
    # main_database.py がDB所有者。main.pyはspoolだけで表示/AI/entryを止めない。
    if not _env_bool("SUMMARY_MAIN_ASYNC_DIRECT_DB_SAVE", False):
        _spool_on_failure(work, interval=int(interval), source=str(source), reason="main_direct_save_disabled")
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
            logger.warning("[SUMMARY MAIN ASYNC SAVE] start key=%s rows=%s direct_enabled=1", key, len(work))
            saved = _direct_sqlite_save(work, int(interval), str(source))
            logger.warning("[SUMMARY MAIN ASYNC SAVE] finished key=%s saved=%s", key, saved)
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
        # 起動時点でmain.py側direct保存を明示的にOFFにする。
        os.environ.setdefault("SUMMARY_MAIN_ASYNC_DIRECT_DB_SAVE", "0")
        os.environ.setdefault("SUMMARY_MAIN_ASYNC_SPOOL_ON_LOCK", "1")
        os.environ.setdefault("SUMMARY_MAIN_SKIP_1M_SAVE_BEFORE_DISPLAY", "1")
        os.environ.setdefault("SUMMARY_MAIN_SKIP_SAVE_BEFORE_DISPLAY_INTERVALS", "1")

        import scheduler_jobs.summary.runner_core as rc

        cur = getattr(rc, "_save_summary_if_owner", None)
        if not callable(cur):
            logger.warning("[SUMMARY MAIN DISPLAY-FIRST SAVE PATCH] target missing")
            return False
        if getattr(cur, "_summary_main_display_first_async_save_patch_v5", False):
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
                    "[SUMMARY MAIN DISPLAY-FIRST SAVE PATCH] display first; main DB direct save disabled by default interval=%s source=%s rows=%s role=%s direct=%s spool=%s",
                    iv,
                    source,
                    rows,
                    os.getenv("SUMMARY_DB_WRITER_ROLE", ""),
                    os.getenv("SUMMARY_MAIN_ASYNC_DIRECT_DB_SAVE", "0"),
                    _env_bool("SUMMARY_MAIN_ASYNC_SPOOL_ON_LOCK", True),
                )
                _submit_async_save(df, int(iv), str(source))
                return None

            return _ORIGINAL_SAVE(df, int(iv), source=source)

        _patched_save_summary_if_owner._summary_main_display_first_async_save_patch = True  # type: ignore[attr-defined]
        _patched_save_summary_if_owner._summary_main_display_first_async_save_patch_v4 = True  # type: ignore[attr-defined]
        _patched_save_summary_if_owner._summary_main_display_first_async_save_patch_v5 = True  # type: ignore[attr-defined]
        _patched_save_summary_if_owner._summary_main_skip_save_display_patch = True  # type: ignore[attr-defined]
        _patched_save_summary_if_owner._original = cur  # type: ignore[attr-defined]
        rc._save_summary_if_owner = _patched_save_summary_if_owner
        _PATCHED = True
        logger.warning(
            "[SUMMARY MAIN DISPLAY-FIRST SAVE PATCH] installed v5 enabled=%s intervals=%s direct_default=%s spool_on_lock=%s",
            _env_bool("SUMMARY_MAIN_SKIP_1M_SAVE_BEFORE_DISPLAY", True),
            sorted(_skip_intervals()),
            os.getenv("SUMMARY_MAIN_ASYNC_DIRECT_DB_SAVE", "0"),
            _env_bool("SUMMARY_MAIN_ASYNC_SPOOL_ON_LOCK", True),
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
