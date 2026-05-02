# ============================================================
# File   : trading/summary/persistence/summary_db_manager.py
# Version: Ver4.0-ULTRA-STABLE-SUMMARY-DB-MANAGER
# ------------------------------------------------------------
# ✔ DB auto create
# ✔ table auto create
# ✔ column auto migration
# ✔ index auto create
# ✔ WAL setup
# ✔ busy_timeout
# ✔ schema validation
# ✔ production stable
# ============================================================

from __future__ import annotations

import sqlite3
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


# ============================================================
# TABLE DEFINITIONS
# ============================================================

SUMMARY_TABLES = {

    "stock_summary_1min": [

        ("symbol","TEXT"),
        ("datetime","TEXT"),

        ("open","REAL"),
        ("high","REAL"),
        ("low","REAL"),
        ("close","REAL"),

        ("volume","REAL"),

        ("ma5","REAL"),
        ("ma25","REAL"),
        ("ma75","REAL"),

        ("ema12","REAL"),
        ("ema26","REAL"),

        ("macd","REAL"),
        ("signal","REAL"),

        ("atr","REAL"),
        ("vwap","REAL"),

        ("slope_atr_scaled","REAL"),
        ("slope_atr_scaled_3m","REAL"),
        ("slope_atr_scaled_5m","REAL"),

        ("score","REAL"),
        ("score_buy","REAL"),
        ("score_sell","REAL"),
        ("score_total","REAL"),
        ("score_slope","REAL"),
        ("score_mtf","REAL"),
    ],

    "stock_summary_3min": [],

    "stock_summary_5min": [],
}


# ============================================================
# SQLite connection
# ============================================================

def _connect(db_path):

    conn = sqlite3.connect(
        db_path,
        timeout=30,
        check_same_thread=False
    )

    cur = conn.cursor()

    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.execute("PRAGMA temp_store=MEMORY")
    cur.execute("PRAGMA cache_size=-200000")
    cur.execute("PRAGMA busy_timeout=5000")

    return conn


# ============================================================
# create table SQL
# ============================================================

def _build_create_table_sql(table, columns):

    cols = []

    for name,ctype in columns:

        cols.append(f"{name} {ctype}")

    cols_sql = ",".join(cols)

    sql = f"""
    CREATE TABLE IF NOT EXISTS {table}
    (
        {cols_sql},
        PRIMARY KEY(symbol,datetime)
    )
    """

    return sql


# ============================================================
# get existing columns
# ============================================================

def _get_existing_columns(conn, table):

    cur = conn.cursor()

    cur.execute(f"PRAGMA table_info({table})")

    rows = cur.fetchall()

    return [r[1] for r in rows]


# ============================================================
# add missing columns
# ============================================================

def _migrate_columns(conn, table, columns):

    existing = _get_existing_columns(conn, table)

    cur = conn.cursor()

    for col,ctype in columns:

        if col not in existing:

            sql = f"ALTER TABLE {table} ADD COLUMN {col} {ctype}"

            logger.info(
                "[SUMMARY DB] adding column %s.%s",
                table,
                col
            )

            cur.execute(sql)

    conn.commit()


# ============================================================
# create index
# ============================================================

def _create_indexes(conn, table):

    cur = conn.cursor()

    cur.execute(
        f"""
        CREATE INDEX IF NOT EXISTS idx_{table}_symbol
        ON {table}(symbol)
        """
    )

    cur.execute(
        f"""
        CREATE INDEX IF NOT EXISTS idx_{table}_datetime
        ON {table}(datetime)
        """
    )

    conn.commit()


# ============================================================
# init table
# ============================================================

def _init_table(conn, table, columns):

    if not columns:
        return

    cur = conn.cursor()

    create_sql = _build_create_table_sql(table, columns)

    cur.execute(create_sql)

    conn.commit()

    _migrate_columns(conn, table, columns)

    _create_indexes(conn, table)


# ============================================================
# initialize summary DB
# ============================================================

def initialize_summary_db(db_path):

    logger.info("[SUMMARY DB] initializing %s", db_path)

    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    conn = _connect(db_path)

    for table, columns in SUMMARY_TABLES.items():

        if table != "stock_summary_1min":

            columns = SUMMARY_TABLES["stock_summary_1min"]

        _init_table(conn, table, columns)

    conn.close()

    logger.info("[SUMMARY DB] ready")