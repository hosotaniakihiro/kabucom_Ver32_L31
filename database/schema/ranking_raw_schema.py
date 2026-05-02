# ============================================================
# File   : database/schema/ranking_raw_schema.py
# Version: PRODUCTION-STABLE-REV1.0-RANKING-RAW-SCHEMA
# ------------------------------------------------------------
# 【概要】
#   ranking_raw_1min schema 管理。
#
# 【目的】
#   - ranking_raw_1min の CREATE TABLE
#   - 既存DBの不足カラム補完
#   - index作成
#   - 古いDBに datetime / snapshot_time 等が無くても起動可能にする
#
# Notes:
#   - ranking_snapshot_schema.py と同じ思想
#   - 既存テーブルへの ALTER TABLE ADD COLUMN では
#     NOT NULL without DEFAULT を避ける
# ============================================================

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from database.sqlite import quote_ident

logger = logging.getLogger(__name__)

RAW_TABLE = "ranking_raw_1min"

RAW_INDEX_DATETIME = "idx_ranking_raw_1min_datetime"
RAW_INDEX_SNAPSHOT_TIME = "idx_ranking_raw_1min_snapshot_time"
RAW_INDEX_SYMBOL_DATETIME = "idx_ranking_raw_1min_symbol_datetime"
RAW_INDEX_TYPE_MARKET_DATETIME = "idx_ranking_raw_1min_type_market_datetime"
RAW_INDEX_INGEST_ID = "idx_ranking_raw_1min_ingest_id"


RANKING_RAW_COLUMNS: list[tuple[str, str]] = [
    ("ingest_id", "TEXT"),
    ("symbol", "TEXT"),
    ("datetime", "TEXT"),
    ("snapshot_time", "TEXT"),
    ("symbolname", "TEXT"),
    ("current_price", "REAL"),
    ("price", "REAL"),
    ("change_percentage", "REAL"),
    ("change_rate", "REAL"),
    ("change_ratio", "REAL"),
    ("trading_volume", "REAL"),
    ("volume", "REAL"),
    ("trading_value", "REAL"),
    ("turnover", "REAL"),
    ("tick_count", "REAL"),
    ("ranking_type", "TEXT"),
    ("rank_type", "TEXT"),
    ("category", "TEXT"),
    ("market", "TEXT"),
    ("exchange", "TEXT"),
    ("source", "TEXT"),
    ("rank", "INTEGER"),
    ("date", "TEXT"),
    ("time", "TEXT"),
    ("raw_json", "TEXT"),
    ("received_at", "TEXT"),
    ("created_at", "TEXT"),
    ("inserted_at", "TEXT DEFAULT CURRENT_TIMESTAMP"),
    ("updated_at", "TEXT DEFAULT CURRENT_TIMESTAMP"),
]


def table_exists(con: sqlite3.Connection, table_name: str = RAW_TABLE) -> bool:
    try:
        row = con.execute(
            """
            SELECT name
              FROM sqlite_master
             WHERE type='table'
               AND name=?
             LIMIT 1
            """,
            (table_name,),
        ).fetchone()
        return row is not None
    except Exception:
        logger.warning(
            "[RANKING RAW SCHEMA] table_exists failed table=%s",
            table_name,
            exc_info=True,
        )
        return False


def index_exists(con: sqlite3.Connection, index_name: str) -> bool:
    try:
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
    except Exception:
        logger.warning(
            "[RANKING RAW SCHEMA] index_exists failed index=%s",
            index_name,
            exc_info=True,
        )
        return False


def get_existing_columns(
    con: sqlite3.Connection,
    table_name: str = RAW_TABLE,
) -> set[str]:
    try:
        if not table_exists(con, table_name):
            return set()

        cur = con.execute(f"PRAGMA table_info({quote_ident(table_name)})")
        return {str(row[1]) for row in cur.fetchall()}

    except Exception:
        logger.warning(
            "[RANKING RAW SCHEMA] get columns failed table=%s",
            table_name,
            exc_info=True,
        )
        return set()


def _relax_add_column_type(column_type: str) -> str:
    """
    SQLite の既存テーブル ADD COLUMN 用に型定義を緩和する。

    SQLite は既存テーブルへ
      ADD COLUMN xxx TEXT NOT NULL
    を DEFAULT なしで追加できないため、NOT NULL を外す。

    DEFAULT CURRENT_TIMESTAMP も ALTER TABLE ADD COLUMN では制約になる場合があるため外す。
    """
    typ = str(column_type or "TEXT")

    upper = typ.upper()

    if "DEFAULT CURRENT_TIMESTAMP" in upper:
        typ = typ.replace("DEFAULT CURRENT_TIMESTAMP", "")
        typ = typ.replace("default current_timestamp", "")
        typ = typ.replace("Default Current_Timestamp", "")

    upper = typ.upper()
    if "NOT NULL" in upper and "DEFAULT" not in upper:
        typ = typ.replace("NOT NULL", "")
        typ = typ.replace("not null", "")
        typ = typ.replace("Not Null", "")

    return " ".join(typ.split()).strip() or "TEXT"


def ensure_column(
    con: sqlite3.Connection,
    table_name: str,
    column_name: str,
    column_type: str,
) -> None:
    existing = get_existing_columns(con, table_name)
    if column_name in existing:
        return

    add_type = _relax_add_column_type(column_type)

    try:
        con.execute(
            f"""
            ALTER TABLE {quote_ident(table_name)}
            ADD COLUMN {quote_ident(column_name)} {add_type}
            """
        )
        logger.warning(
            "[RANKING RAW SCHEMA] added missing column table=%s column=%s type=%s",
            table_name,
            column_name,
            add_type,
        )

    except sqlite3.OperationalError as exc:
        if "duplicate column" in str(exc).lower():
            return

        logger.warning(
            "[RANKING RAW SCHEMA] add column failed table=%s column=%s type=%s err=%s",
            table_name,
            column_name,
            add_type,
            exc,
            exc_info=True,
        )
        raise


def patch_ranking_raw_schema(con: sqlite3.Connection) -> None:
    """
    既存 ranking_raw_1min に不足カラムがあれば補完する。
    """
    ensure_ranking_raw_table(con)

    existing = get_existing_columns(con, RAW_TABLE)
    if not existing:
        return

    patched = 0

    for col, typ in RANKING_RAW_COLUMNS:
        if col not in existing:
            ensure_column(con, RAW_TABLE, col, typ)
            existing.add(col)
            patched += 1

    if patched:
        logger.warning(
            "[RANKING RAW SCHEMA] schema patch completed table=%s patched_columns=%s",
            RAW_TABLE,
            patched,
        )


def ensure_ranking_raw_table(con: sqlite3.Connection) -> None:
    """
    ranking_raw_1min を作成し、必要な index を作成する。

    既存テーブルがある場合:
      CREATE TABLE IF NOT EXISTS は既存テーブルを変更しないため、
      patch_ranking_raw_schema() 側で不足カラム補完する。
    """
    con.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {quote_ident(RAW_TABLE)} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ingest_id TEXT,
            symbol TEXT,
            datetime TEXT,
            snapshot_time TEXT,
            symbolname TEXT,
            current_price REAL,
            price REAL,
            change_percentage REAL,
            change_rate REAL,
            change_ratio REAL,
            trading_volume REAL,
            volume REAL,
            trading_value REAL,
            turnover REAL,
            tick_count REAL,
            ranking_type TEXT,
            rank_type TEXT,
            category TEXT,
            market TEXT,
            exchange TEXT,
            source TEXT,
            rank INTEGER,
            date TEXT,
            time TEXT,
            raw_json TEXT,
            received_at TEXT,
            created_at TEXT,
            inserted_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    # CREATE TABLE IF NOT EXISTS では既存テーブルは変わらないため、
    # index 作成前に必ず不足カラムを補完する。
    existing = get_existing_columns(con, RAW_TABLE)
    patched = 0
    for col, typ in RANKING_RAW_COLUMNS:
        if col not in existing:
            ensure_column(con, RAW_TABLE, col, typ)
            existing.add(col)
            patched += 1

    if patched:
        logger.warning(
            "[RANKING RAW SCHEMA] ensure added missing columns table=%s patched=%s",
            RAW_TABLE,
            patched,
        )

    ensure_ranking_raw_indexes(con)


def ensure_ranking_raw_indexes(con: sqlite3.Connection) -> None:
    """
    ranking_raw_1min の検索用 index を作成する。
    UNIQUE制約は張らない。
    raw は履歴として重複許容し、ingest_id で識別する。
    """
    con.execute(
        f"""
        CREATE INDEX IF NOT EXISTS {quote_ident(RAW_INDEX_DATETIME)}
        ON {quote_ident(RAW_TABLE)}(datetime)
        """
    )

    con.execute(
        f"""
        CREATE INDEX IF NOT EXISTS {quote_ident(RAW_INDEX_SNAPSHOT_TIME)}
        ON {quote_ident(RAW_TABLE)}(snapshot_time)
        """
    )

    con.execute(
        f"""
        CREATE INDEX IF NOT EXISTS {quote_ident(RAW_INDEX_SYMBOL_DATETIME)}
        ON {quote_ident(RAW_TABLE)}(symbol, datetime)
        """
    )

    con.execute(
        f"""
        CREATE INDEX IF NOT EXISTS {quote_ident(RAW_INDEX_TYPE_MARKET_DATETIME)}
        ON {quote_ident(RAW_TABLE)}(ranking_type, market, datetime)
        """
    )

    con.execute(
        f"""
        CREATE INDEX IF NOT EXISTS {quote_ident(RAW_INDEX_INGEST_ID)}
        ON {quote_ident(RAW_TABLE)}(ingest_id)
        """
    )


def delete_null_key_rows(con: sqlite3.Connection) -> int:
    """
    symbol / datetime が空の raw 行を削除する。
    """
    try:
        if not table_exists(con, RAW_TABLE):
            return 0

        before = int(
            con.execute(
                f"SELECT COUNT(*) FROM {quote_ident(RAW_TABLE)}"
            ).fetchone()[0]
        )

        con.execute(
            f"""
            DELETE FROM {quote_ident(RAW_TABLE)}
             WHERE symbol IS NULL
                OR datetime IS NULL
                OR TRIM(CAST(symbol AS TEXT)) = ''
                OR TRIM(CAST(datetime AS TEXT)) = ''
            """
        )

        after = int(
            con.execute(
                f"SELECT COUNT(*) FROM {quote_ident(RAW_TABLE)}"
            ).fetchone()[0]
        )

        deleted = before - after

        if deleted > 0:
            logger.warning(
                "[RANKING RAW SCHEMA] null-key rows deleted before=%s after=%s deleted=%s",
                before,
                after,
                deleted,
            )

        return deleted

    except Exception:
        logger.warning(
            "[RANKING RAW SCHEMA] delete null key rows failed",
            exc_info=True,
        )
        return 0


def get_ranking_raw_schema_columns(con: sqlite3.Connection) -> list[str]:
    try:
        return sorted(get_existing_columns(con, RAW_TABLE))
    except Exception:
        return []


def get_ranking_raw_schema_status(con: sqlite3.Connection) -> dict[str, Any]:
    try:
        cols = get_existing_columns(con, RAW_TABLE)
        expected = {c for c, _ in RANKING_RAW_COLUMNS}
        missing = sorted(expected - cols)

        count = None
        if table_exists(con, RAW_TABLE):
            try:
                count = int(
                    con.execute(
                        f"SELECT COUNT(*) FROM {quote_ident(RAW_TABLE)}"
                    ).fetchone()[0]
                )
            except Exception:
                count = None

        return {
            "table": RAW_TABLE,
            "exists": table_exists(con, RAW_TABLE),
            "columns": sorted(cols),
            "missing_columns": missing,
            "row_count": count,
            "ok": len(missing) == 0,
        }

    except Exception as e:
        return {
            "table": RAW_TABLE,
            "exists": False,
            "columns": [],
            "missing_columns": [],
            "row_count": None,
            "ok": False,
            "error": str(e),
        }


__all__ = [
    "RAW_TABLE",
    "RANKING_RAW_COLUMNS",
    "ensure_ranking_raw_table",
    "patch_ranking_raw_schema",
    "ensure_ranking_raw_indexes",
    "delete_null_key_rows",
    "get_existing_columns",
    "get_ranking_raw_schema_columns",
    "get_ranking_raw_schema_status",
]