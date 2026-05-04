# ============================================================
# File   : database/schema/yahoo_tracking_state_schema.py
# Version: PRODUCTION-STABLE-REV1.0-YAHOO-TRACKING-STATE-SCHEMA
# ------------------------------------------------------------
# Purpose:
#   Rankingに一度でも出た銘柄を当日中Yahoo追跡対象にするための
#   SQLite schema / migration helpers.
#
# Guarantees:
#   - ADD ONLY migration
#   - Existing data is never dropped
#   - yahoo_1min and yahoo_tracking_state are both ensured
#   - Unique/lookup indexes are ensured
#   - NAS SQLite lock retry compatible
# ============================================================

from __future__ import annotations

import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from database.paths.yahoo_paths import get_yahoo_1min_db_path
from database.sqlite import DEFAULT_BUSY_TIMEOUT_MS, is_lock_error, lock_sleep_seconds, quote_ident

logger = logging.getLogger(__name__)

YAHOO_1MIN_TABLE = "yahoo_1min"
YAHOO_TRACKING_STATE_TABLE = "yahoo_tracking_state"

MAX_SCHEMA_RETRY = 8


YAHOO_1MIN_COLUMNS: List[tuple[str, str]] = [
    ("symbol", "TEXT NOT NULL"),
    ("datetime", "TEXT NOT NULL"),
    ("date", "TEXT"),
    ("time", "TEXT"),
    ("open", "REAL"),
    ("high", "REAL"),
    ("low", "REAL"),
    ("close", "REAL"),
    ("open_price", "REAL"),
    ("high_price", "REAL"),
    ("low_price", "REAL"),
    ("close_price", "REAL"),
    ("volume", "REAL DEFAULT 0"),
    ("source", "TEXT"),
    ("updated_at", "TEXT"),
]


YAHOO_TRACKING_STATE_COLUMNS: List[tuple[str, str]] = [
    ("symbol", "TEXT NOT NULL"),
    ("trade_date", "TEXT NOT NULL"),
    ("symbolname", "TEXT"),
    ("first_seen_at", "TEXT"),
    ("last_seen_at", "TEXT"),
    ("last_yahoo_downloaded_at", "TEXT"),
    ("last_summary_calculated_at", "TEXT"),
    ("last_3min_calculated_at", "TEXT"),
    ("last_5min_calculated_at", "TEXT"),
    ("last_yahoo_db_at", "TEXT"),
    ("last_summary_1min_db_at", "TEXT"),
    ("last_summary_3min_db_at", "TEXT"),
    ("last_summary_5min_db_at", "TEXT"),
    ("ranking_hit_count", "INTEGER DEFAULT 0"),
    ("first_rank", "REAL"),
    ("best_rank", "REAL"),
    ("last_rank", "REAL"),
    ("last_price", "REAL"),
    ("last_volume", "REAL"),
    ("ranking_type", "TEXT"),
    ("market", "TEXT"),
    ("source", "TEXT"),
    ("active", "INTEGER DEFAULT 1"),
    ("created_at", "TEXT"),
    ("updated_at", "TEXT"),
]


def _connect(db_path: str | Path) -> sqlite3.Connection:
    db_path = str(db_path)
    parent = os.path.dirname(db_path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    con = sqlite3.connect(
        db_path,
        timeout=max(10.0, float(DEFAULT_BUSY_TIMEOUT_MS) / 1000.0),
        check_same_thread=False,
        isolation_level=None,
    )
    try:
        con.execute(f"PRAGMA busy_timeout={int(DEFAULT_BUSY_TIMEOUT_MS)}")
    except Exception:
        pass
    try:
        con.execute("PRAGMA synchronous=NORMAL")
    except Exception:
        pass
    return con


def _close_quietly(con: Optional[sqlite3.Connection]) -> None:
    if con is None:
        return
    try:
        con.close()
    except Exception:
        pass


def _rollback_quietly(con: Optional[sqlite3.Connection]) -> None:
    if con is None:
        return
    try:
        con.execute("ROLLBACK")
    except Exception:
        pass


def _table_exists(con: sqlite3.Connection, table: str) -> bool:
    row = con.execute(
        """
        SELECT name
          FROM sqlite_master
         WHERE type='table'
           AND name=?
         LIMIT 1
        """,
        (table,),
    ).fetchone()
    return row is not None


def _index_exists(con: sqlite3.Connection, index_name: str) -> bool:
    row = con.execute(
        """
        SELECT name
          FROM sqlite_master
         WHERE type='index'
           AND name=?
         LIMIT 1
        """,
        (index_name,),
    ).fetchone()
    return row is not None


def get_existing_columns(con: sqlite3.Connection, table: str) -> Dict[str, Dict[str, Any]]:
    if not _table_exists(con, table):
        return {}

    cols: Dict[str, Dict[str, Any]] = {}
    for row in con.execute(f"PRAGMA table_info({quote_ident(table)})").fetchall():
        cols[str(row[1])] = {
            "cid": row[0],
            "name": row[1],
            "type": row[2],
            "notnull": row[3],
            "dflt_value": row[4],
            "pk": row[5],
        }
    return cols


def _create_table_sql(table: str, columns: Iterable[tuple[str, str]], pk_cols: Iterable[str]) -> str:
    col_lines = [f"            {quote_ident(name)} {typ}" for name, typ in columns]
    col_sql = ",\n".join(col_lines)
    pk = ", ".join(quote_ident(c) for c in pk_cols)
    table_q = quote_ident(table)
    return (
        f"CREATE TABLE IF NOT EXISTS {table_q} (\n"
        f"{col_sql},\n"
        f"            PRIMARY KEY ({pk})\n"
        f")"
    )



def _ensure_columns(con: sqlite3.Connection, table: str, columns: Iterable[tuple[str, str]]) -> int:
    existing = get_existing_columns(con, table)
    added = 0
    for name, typ in columns:
        if name in existing:
            continue
        con.execute(f"ALTER TABLE {quote_ident(table)} ADD COLUMN {quote_ident(name)} {typ}")
        added += 1
        logger.info("[YAHOO SCHEMA] column added table=%s column=%s type=%s", table, name, typ)
    return added


def _ensure_index(con: sqlite3.Connection, index_name: str, sql: str) -> None:
    if not _index_exists(con, index_name):
        con.execute(sql)
        logger.info("[YAHOO SCHEMA] index ensured index=%s", index_name)


def ensure_yahoo_1min_table(con: sqlite3.Connection) -> None:
    con.execute(_create_table_sql(YAHOO_1MIN_TABLE, YAHOO_1MIN_COLUMNS, ("symbol", "datetime")))
    _ensure_columns(con, YAHOO_1MIN_TABLE, YAHOO_1MIN_COLUMNS)
    _ensure_index(
        con,
        "idx_yahoo_1min_date_symbol_datetime",
        f"CREATE INDEX IF NOT EXISTS idx_yahoo_1min_date_symbol_datetime ON {quote_ident(YAHOO_1MIN_TABLE)} (date, symbol, datetime)",
    )
    _ensure_index(
        con,
        "idx_yahoo_1min_symbol_date",
        f"CREATE INDEX IF NOT EXISTS idx_yahoo_1min_symbol_date ON {quote_ident(YAHOO_1MIN_TABLE)} (symbol, date)",
    )


def ensure_yahoo_tracking_state_table(con: sqlite3.Connection) -> None:
    con.execute(
        _create_table_sql(
            YAHOO_TRACKING_STATE_TABLE,
            YAHOO_TRACKING_STATE_COLUMNS,
            ("symbol", "trade_date"),
        )
    )
    _ensure_columns(con, YAHOO_TRACKING_STATE_TABLE, YAHOO_TRACKING_STATE_COLUMNS)
    _ensure_index(
        con,
        "idx_yahoo_tracking_state_trade_date_active",
        f"CREATE INDEX IF NOT EXISTS idx_yahoo_tracking_state_trade_date_active ON {quote_ident(YAHOO_TRACKING_STATE_TABLE)} (trade_date, active)",
    )
    _ensure_index(
        con,
        "idx_yahoo_tracking_state_last_yahoo",
        f"CREATE INDEX IF NOT EXISTS idx_yahoo_tracking_state_last_yahoo ON {quote_ident(YAHOO_TRACKING_STATE_TABLE)} (trade_date, last_yahoo_downloaded_at)",
    )
    _ensure_index(
        con,
        "idx_yahoo_tracking_state_last_summary",
        f"CREATE INDEX IF NOT EXISTS idx_yahoo_tracking_state_last_summary ON {quote_ident(YAHOO_TRACKING_STATE_TABLE)} (trade_date, last_summary_calculated_at)",
    )


def ensure_yahoo_schema(
    db_path: str | Path | None = None,
    *,
    trade_date: Any = None,
    ensure_1min: bool = True,
    ensure_tracking: bool = True,
) -> str:
    """
    Ensure Yahoo DB tables and indexes.

    Returns:
        Resolved DB path.
    """
    resolved = str(db_path or get_yahoo_1min_db_path(trade_date))
    last_err: Exception | None = None

    for attempt in range(1, MAX_SCHEMA_RETRY + 1):
        con: sqlite3.Connection | None = None
        try:
            con = _connect(resolved)
            con.execute("BEGIN IMMEDIATE")
            if ensure_1min:
                ensure_yahoo_1min_table(con)
            if ensure_tracking:
                ensure_yahoo_tracking_state_table(con)
            con.execute("COMMIT")
            logger.info("[YAHOO SCHEMA] ensured db=%s", resolved)
            return resolved

        except Exception as e:
            last_err = e
            _rollback_quietly(con)
            if is_lock_error(e) and attempt < MAX_SCHEMA_RETRY:
                sleep_s = lock_sleep_seconds(attempt, 0.35)
                logger.warning(
                    "[YAHOO SCHEMA] locked retry db=%s attempt=%s/%s sleep=%.2fs err=%s",
                    resolved,
                    attempt,
                    MAX_SCHEMA_RETRY,
                    sleep_s,
                    str(e).splitlines()[0] if str(e) else type(e).__name__,
                )
                time.sleep(sleep_s)
                continue
            logger.exception("[YAHOO SCHEMA] ensure failed db=%s", resolved)
            break
        finally:
            _close_quietly(con)

    if last_err is not None:
        raise last_err
    return resolved


__all__ = [
    "YAHOO_1MIN_TABLE",
    "YAHOO_TRACKING_STATE_TABLE",
    "YAHOO_1MIN_COLUMNS",
    "YAHOO_TRACKING_STATE_COLUMNS",
    "get_existing_columns",
    "ensure_yahoo_1min_table",
    "ensure_yahoo_tracking_state_table",
    "ensure_yahoo_schema",
]
