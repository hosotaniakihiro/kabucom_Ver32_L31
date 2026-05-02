# ============================================================
# File   : trading/summary/persistence/schema_utils.py
# Version: Ver1.1-PRODUCTION-SCHEMA-UTILS-WAL-STRICT-FINAL
# ------------------------------------------------------------
# ✔ Ver1.0 全機能保持
# ✔ DB schema検出
# ✔ SQLite / DuckDB互換
# ✔ UNIQUE KEY自動検出
# ✔ existing column filter
# ✔ WALモード設定
# ✔ index検出
# ✔ table existence check
# ✔ future column耐性
# ✔ production safe
# ✔ index検出の安全性強化
# ✔ identifier quote強化
# ✔ table/index pragma の安全呼び出し
# ✔ WAL設定ログ強化
# ============================================================

from __future__ import annotations

import logging
import pandas as pd

from sqlalchemy import inspect
from sqlalchemy import text

logger = logging.getLogger(__name__)


def _quote_sqlite_ident(name: str) -> str:
    s = "" if name is None else str(name)
    s = s.replace('"', '""')
    return f'"{s}"'


# ============================================================
# TABLE EXISTENCE
# ============================================================

def table_exists(conn, table_name: str) -> bool:
    try:
        inspector = inspect(conn)
        tables = inspector.get_table_names()
        return table_name in tables
    except Exception:
        logger.exception("[SCHEMA] table existence check failed")
        return False


# ============================================================
# GET TABLE COLUMNS
# ============================================================

def get_table_columns(conn, table_name: str) -> set:
    try:
        inspector = inspect(conn)
        columns = inspector.get_columns(table_name)
        return {col["name"] for col in columns}
    except Exception:
        logger.exception("[SCHEMA] get_table_columns failed")
        return set()


# ============================================================
# FILTER EXISTING COLUMNS
# ============================================================

def filter_existing_columns(
    df: pd.DataFrame,
    conn,
    table_name: str
) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    try:
        existing_cols = get_table_columns(conn, table_name)

        if not existing_cols:
            logger.warning("[SCHEMA] existing columns not found: %s", table_name)
            return df

        valid_cols = [c for c in df.columns if c in existing_cols]

        if not valid_cols:
            logger.warning(
                "[SCHEMA] no matching columns for table %s",
                table_name
            )
            return pd.DataFrame()

        dropped_cols = [c for c in df.columns if c not in existing_cols]
        if dropped_cols:
            logger.info(
                "[SCHEMA] dropped non-existing columns for %s: %s",
                table_name,
                dropped_cols,
            )

        return df[valid_cols].copy()

    except Exception:
        logger.exception("[SCHEMA] column filter failed")
        return df


# ============================================================
# UNIQUE CONFLICT DETECTION
# ============================================================

def detect_unique_conflict(conn, table_name: str) -> str:
    """
    UPSERT用 UNIQUE KEY 検出
    """

    try:
        rows = conn.execute(
            text(f"PRAGMA index_list({_quote_sqlite_ident(table_name)})")
        ).fetchall()

        for row in rows:
            # row structure:
            # seq, name, unique, origin, partial
            if len(row) < 3:
                continue

            if row[2] == 0:
                continue

            index_name = row[1]

            cols = conn.execute(
                text(f"PRAGMA index_info({_quote_sqlite_ident(index_name)})")
            ).fetchall()

            colnames = [c[2] for c in cols if len(c) > 2 and c[2] is not None]

            # preferred conflict keys
            if {"symbol", "date", "time_range"}.issubset(set(colnames)):
                return "symbol, date, time_range"

            if {"symbol", "datetime"}.issubset(set(colnames)):
                return "symbol, datetime"

    except Exception:
        logger.exception("[SCHEMA] UNIQUE detect failed")

    return "symbol, datetime"


# ============================================================
# WAL MODE
# ============================================================

def ensure_wal_mode(conn):
    try:
        mode_row = conn.exec_driver_sql("PRAGMA journal_mode=WAL").fetchone()
        conn.exec_driver_sql("PRAGMA synchronous=NORMAL")
        conn.exec_driver_sql("PRAGMA busy_timeout=60000")
        conn.exec_driver_sql("PRAGMA temp_store=MEMORY")
        conn.exec_driver_sql("PRAGMA foreign_keys=ON")

        mode = None
        if mode_row and len(mode_row) > 0:
            mode = mode_row[0]

        logger.debug("[SCHEMA] WAL mode ensured: %s", mode)
    except Exception:
        logger.warning("[SCHEMA] WAL mode set failed")


# ============================================================
# INDEX LIST
# ============================================================

def get_indexes(conn, table_name: str):
    try:
        rows = conn.execute(
            text(f"PRAGMA index_list({_quote_sqlite_ident(table_name)})")
        ).fetchall()

        indexes = []

        for row in rows:
            indexes.append({
                "name": row[1] if len(row) > 1 else None,
                "unique": bool(row[2]) if len(row) > 2 else False
            })

        return indexes

    except Exception:
        logger.exception("[SCHEMA] get_indexes failed")
        return []


# ============================================================
# DEBUG SCHEMA
# ============================================================

def debug_schema(conn, table_name: str):
    try:
        cols = get_table_columns(conn, table_name)
        indexes = get_indexes(conn, table_name)

        logger.info(
            "[SCHEMA DEBUG] table=%s columns=%d indexes=%d",
            table_name,
            len(cols),
            len(indexes)
        )
    except Exception:
        logger.exception("[SCHEMA] debug failed")