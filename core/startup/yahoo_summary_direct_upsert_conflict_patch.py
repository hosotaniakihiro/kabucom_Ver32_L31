# -*- coding: utf-8 -*-
"""
Patch Yahoo complement direct SQLite summary saver to tolerate existing rows.

Problem observed:
    sqlite3.IntegrityError: UNIQUE constraint failed:
    stock_summary_3min.symbol, stock_summary_3min.datetime

The original direct fallback did DELETE -> INSERT using a logical key selected from
available dataframe columns.  On some 3m/5m schemas the real UNIQUE index is
(symbol, datetime), while the cleanup/delete key may prefer (symbol, date,
time_range).  When the row already exists by (symbol, datetime), INSERT can fail.

This patch replaces the direct fallback with SQLite UPSERT:
    INSERT ... ON CONFLICT(<actual unique key>) DO UPDATE SET ...

It is intentionally narrow and only patches
trading.yahoo.pipeline.complement.save._direct_sqlite_upsert_summary_df.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from typing import List, Optional

import pandas as pd

logger = logging.getLogger(__name__)

VERSION = "REV1-YAHOO-SUMMARY-DIRECT-UPSERT-CONFLICT+PUSH-FALLBACK-FIX"
_INSTALLED = False
_ORIG_DIRECT = None


def _q(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _unique_indexes(con: sqlite3.Connection, table: str) -> List[List[str]]:
    indexes: List[List[str]] = []
    try:
        for row in con.execute(f"PRAGMA index_list({_q(table)})").fetchall():
            # row: seq, name, unique, origin, partial
            if len(row) < 3 or int(row[2] or 0) != 1:
                continue
            idx_name = str(row[1])
            cols = [str(r[2]) for r in con.execute(f"PRAGMA index_info({_q(idx_name)})").fetchall() if len(r) >= 3]
            if cols:
                indexes.append(cols)
    except Exception:
        logger.debug("[YAHOO SAVE CONFLICT PATCH] unique index inspect failed table=%s", table, exc_info=True)
    return indexes


def _choose_conflict_cols(con: sqlite3.Connection, table: str, work: pd.DataFrame, table_cols: List[str], interval: int, save_mod) -> List[str]:
    work_cols = set(map(str, work.columns))
    table_col_set = set(map(str, table_cols))

    # Prefer the real UNIQUE index when it is present in both dataframe and table.
    preferred = [
        ["symbol", "datetime"],
        ["symbol", "date", "time_range"],
        ["symbol", "date", "time"],
    ]
    unique_indexes = _unique_indexes(con, table)
    for wanted in preferred:
        for idx_cols in unique_indexes:
            if idx_cols == wanted and set(idx_cols).issubset(work_cols) and set(idx_cols).issubset(table_col_set):
                return idx_cols

    for idx_cols in unique_indexes:
        if set(idx_cols).issubset(work_cols) and set(idx_cols).issubset(table_col_set):
            return idx_cols

    # Fallback to the module's logical key picker.
    try:
        key_cols = save_mod._key_columns_for_table(work, interval=interval, table_cols=table_cols)
        if key_cols:
            return list(key_cols)
    except Exception:
        pass
    return []


def _patched_direct_sqlite_upsert_summary_df(df, *, interval: int, db_path: Optional[str] = None) -> int:
    import trading.yahoo.pipeline.complement.save as save_mod

    out = save_mod.safe_df(df)
    if out.empty:
        return 0

    table = save_mod.summary_table_for_interval(interval)
    db_path = db_path or save_mod.get_summary_db_path(date_yyyymmdd=save_mod._detect_date_yyyymmdd(out))

    con = None
    try:
        if not os.path.exists(str(db_path)):
            logger.error("[YAHOO SAVE][DIRECT][UPSERT] summary db not found interval=%s table=%s db=%s", interval, table, db_path)
            return 0

        timeout = max(float(save_mod.YAHOO_SUMMARY_LOCK_TIMEOUT_SEC), 15.0)
        con = sqlite3.connect(str(db_path), timeout=timeout)
        con.execute("PRAGMA busy_timeout = 15000")
        try:
            con.execute("PRAGMA journal_mode = WAL")
            con.execute("PRAGMA synchronous = NORMAL")
        except Exception:
            pass

        exists = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        if exists is None:
            logger.error("[YAHOO SAVE][DIRECT][UPSERT] summary table not found interval=%s table=%s db=%s", interval, table, db_path)
            return 0

        table_cols = [str(r[1]) for r in con.execute(f"PRAGMA table_info({_q(table)})").fetchall()]
        if not table_cols:
            logger.error("[YAHOO SAVE][DIRECT][UPSERT] no table columns interval=%s table=%s db=%s", interval, table, db_path)
            return 0

        work = save_mod._normalize_for_sqlite(out)
        if "last_update" in table_cols and "last_update" not in work.columns:
            work["last_update"] = pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S")
        if "source" in table_cols and "source" not in work.columns:
            work["source"] = "summary_recovery_yahoo_1m" if int(interval) == 1 else f"summary_recovery_yahoo_resample_{int(interval)}m"

        cols = [c for c in table_cols if c in work.columns and c != "id"]
        if not cols:
            logger.error("[YAHOO SAVE][DIRECT][UPSERT] no matching columns interval=%s table=%s df_cols=%s", interval, table, list(work.columns))
            return 0

        conflict_cols = _choose_conflict_cols(con, table, work, table_cols, int(interval), save_mod)
        if not conflict_cols:
            logger.error("[YAHOO SAVE][DIRECT][UPSERT] no conflict columns interval=%s table=%s df_cols=%s table_cols=%s", interval, table, list(work.columns), table_cols)
            return 0

        work = work.dropna(subset=conflict_cols).drop_duplicates(subset=conflict_cols, keep="last").reset_index(drop=True)
        if work.empty:
            logger.warning("[YAHOO SAVE][DIRECT][UPSERT] no rows after conflict key cleanup interval=%s table=%s conflict=%s", interval, table, conflict_cols)
            return 0

        placeholders = ",".join(["?"] * len(cols))
        col_sql = ",".join(_q(c) for c in cols)
        conflict_sql = ",".join(_q(c) for c in conflict_cols)
        update_cols = [c for c in cols if c not in conflict_cols]

        if update_cols:
            update_sql = ",".join(f"{_q(c)}=excluded.{_q(c)}" for c in update_cols)
            insert_sql = (
                f"INSERT INTO {_q(table)} ({col_sql}) VALUES ({placeholders}) "
                f"ON CONFLICT({conflict_sql}) DO UPDATE SET {update_sql}"
            )
        else:
            insert_sql = (
                f"INSERT INTO {_q(table)} ({col_sql}) VALUES ({placeholders}) "
                f"ON CONFLICT({conflict_sql}) DO NOTHING"
            )

        records = [
            tuple(save_mod._to_sqlite_value(row.get(c)) for c in cols)
            for _, row in work[cols].iterrows()
        ]
        con.executemany(insert_sql, records)
        con.commit()

        logger.info(
            "[YAHOO SAVE][DIRECT][UPSERT] summary sqlite upsert done interval=%s table=%s rows=%s conflict=%s db=%s source=%s latest=%s cols=%s",
            interval,
            table,
            len(records),
            conflict_cols,
            db_path,
            (work["source"].iloc[0] if "source" in work.columns and not work.empty else None),
            (work["datetime"].max() if "datetime" in work.columns and not work.empty else None),
            len(cols),
        )
        return int(len(records))

    except sqlite3.OperationalError as e:
        msg = str(e).lower()
        if "locked" in msg or "busy" in msg:
            logger.warning("[YAHOO SAVE][DIRECT][UPSERT] database busy/locked interval=%s table=%s db=%s err=%s", interval, table, db_path, e)
        else:
            logger.exception("[YAHOO SAVE][DIRECT][UPSERT] sqlite operational error interval=%s table=%s db=%s", interval, table, db_path)
        if con is not None:
            try:
                con.rollback()
            except Exception:
                pass
        return 0
    except Exception:
        if con is not None:
            try:
                con.rollback()
            except Exception:
                pass
        logger.exception("[YAHOO SAVE][DIRECT][UPSERT] failed interval=%s table=%s db=%s", interval, table, db_path)
        return 0
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:
                pass


def _install_push_summary_and_price_fix() -> bool:
    try:
        from core.startup import push_summary_fallback_and_active_price_patch as mod
        ok = bool(mod.install())
        logger.warning("[YAHOO SAVE CONFLICT PATCH] chained push summary/active price patch ok=%s version=%s", ok, getattr(mod, "VERSION", "unknown"))
        return ok
    except Exception:
        logger.exception("[YAHOO SAVE CONFLICT PATCH] chained push summary/active price patch failed")
        return False


def install() -> bool:
    global _INSTALLED, _ORIG_DIRECT
    if _INSTALLED:
        return True
    chained_ok = _install_push_summary_and_price_fix()
    try:
        import trading.yahoo.pipeline.complement.save as save_mod

        _ORIG_DIRECT = getattr(save_mod, "_direct_sqlite_upsert_summary_df", None)
        save_mod._direct_sqlite_upsert_summary_df = _patched_direct_sqlite_upsert_summary_df
        _INSTALLED = True
        logger.warning("[YAHOO SAVE CONFLICT PATCH] installed version=%s chained_push_fix=%s", VERSION, chained_ok)
        return True
    except Exception:
        logger.exception("[YAHOO SAVE CONFLICT PATCH] install failed version=%s", VERSION)
        return bool(chained_ok)


__all__ = ["install", "VERSION"]
