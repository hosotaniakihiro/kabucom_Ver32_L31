# ============================================================
# File   : database/schema/ranking_snapshot_schema.py
# Version: PRODUCTION-STABLE-REV2.2-RANKING-SNAPSHOT-TECHNICAL-COLUMNS
# ------------------------------------------------------------
# 【概要】
#   ranking_snapshot_1min schema 管理。
#
# 【REV2.2 修正内容】
#   - ranking_snapshot_1min にランキング由来の 1m/3m/5m テクニカル列を追加
#   - 各足の ma5 / ma25 / ma75 / rsi / macd / signal / slope / slope_atr_scaled 等を保存可能にする
#   - 既存DBは patch_ranking_snapshot_schema() 実行時に不足列を ALTER TABLE で追加
#   - upsert本文は22列互換を維持し、テクニカル列は後段patchでUPDATEする
# ============================================================

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from database.sqlite import quote_ident

logger = logging.getLogger(__name__)

SNAPSHOT_TABLE = "ranking_snapshot_1min"
SNAPSHOT_UNIQUE_INDEX = "uq_ranking_snapshot_1min_symbol_datetime_type_market"

SNAPSHOT_UNIQUE_INDEX_SNAPSHOT_TIME = (
    "uq_ranking_snapshot_1min_symbol_snapshot_type_market"
)


def _technical_columns() -> list[tuple[str, str]]:
    cols: list[tuple[str, str]] = []
    for tf in ("1m", "3m", "5m"):
        cols.extend([
            (f"ma5_{tf}", "REAL"),
            (f"ma25_{tf}", "REAL"),
            (f"ma75_{tf}", "REAL"),
            (f"ma5_slope_{tf}", "REAL"),
            (f"ma25_slope_{tf}", "REAL"),
            (f"ma75_slope_{tf}", "REAL"),
            (f"ma5_slope_pct_{tf}", "REAL"),
            (f"ma25_slope_pct_{tf}", "REAL"),
            (f"ma75_slope_pct_{tf}", "REAL"),
            (f"rsi_{tf}", "REAL"),
            (f"macd_{tf}", "REAL"),
            (f"signal_{tf}", "REAL"),
            (f"macd_hist_{tf}", "REAL"),
            (f"slope_{tf}", "REAL"),
            (f"slope_pct_{tf}", "REAL"),
            (f"atr_{tf}", "REAL"),
            (f"slope_atr_scaled_{tf}", "REAL"),
            (f"price_change_pct_{tf}", "REAL"),
            (f"volume_sma5_{tf}", "REAL"),
            (f"volume_sma25_{tf}", "REAL"),
            (f"volume_ratio5_{tf}", "REAL"),
            (f"technical_ready_{tf}", "INTEGER DEFAULT 0"),
        ])
    cols.extend([
        ("technical_updated_at", "TEXT"),
        ("technical_source", "TEXT"),
    ])
    return cols


RANKING_SNAPSHOT_COLUMNS: list[tuple[str, str]] = [
    ("symbol", "TEXT NOT NULL"),
    ("datetime", "TEXT NOT NULL"),
    ("snapshot_time", "TEXT NOT NULL"),
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
    ("ranking_type", "TEXT NOT NULL DEFAULT ''"),
    ("rank_type", "TEXT"),
    ("rank_type_id", "INTEGER"),
    ("category", "TEXT"),
    ("market", "TEXT NOT NULL DEFAULT ''"),
    ("exchange", "TEXT"),
    ("source", "TEXT"),
    ("rank", "INTEGER"),
    ("rank_position", "INTEGER"),
    ("best_rank", "INTEGER"),
    ("price_delta_1m", "REAL"),
    ("volume_delta_1m", "REAL"),
    ("volume_speed", "REAL"),
    ("minute_of_day", "INTEGER"),
    ("date", "TEXT"),
    ("time", "TEXT"),
    ("raw_json", "TEXT"),
    ("ingest_id", "TEXT"),
    ("received_at", "TEXT"),
    ("created_at", "TEXT"),
    ("inserted_at", "TEXT DEFAULT CURRENT_TIMESTAMP"),
    ("updated_at", "TEXT DEFAULT CURRENT_TIMESTAMP"),
    *_technical_columns(),
]


def get_existing_columns(con: sqlite3.Connection, table_name: str) -> set[str]:
    try:
        cur = con.execute(f"PRAGMA table_info({quote_ident(table_name)})")
        return {str(row[1]) for row in cur.fetchall()}
    except Exception:
        logger.warning(
            "[RANKING SNAPSHOT SCHEMA] get columns failed table=%s",
            table_name,
            exc_info=True,
        )
        return set()


def _table_exists(con: sqlite3.Connection, table_name: str) -> bool:
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


def _sanitize_add_column_type(column_type: str) -> str:
    """
    SQLite の ALTER TABLE ADD COLUMN で安全に使える型へ変換する。
    """
    add_type = str(column_type or "TEXT").strip()
    upper = add_type.upper()

    if "PRIMARY KEY" in upper:
        add_type = add_type.replace("PRIMARY KEY", "")
        add_type = add_type.replace("primary key", "")

    upper = add_type.upper()

    if "DEFAULT CURRENT_TIMESTAMP" in upper:
        add_type = add_type.replace("DEFAULT CURRENT_TIMESTAMP", "")
        add_type = add_type.replace("default current_timestamp", "")
        add_type = add_type.replace("Default Current_Timestamp", "")

    upper = add_type.upper()

    if "NOT NULL" in upper and "DEFAULT" not in upper:
        add_type = add_type.replace("NOT NULL", "")
        add_type = add_type.replace("not null", "")
        add_type = add_type.replace("Not Null", "")

    add_type = " ".join(add_type.split())
    return add_type or "TEXT"


def ensure_column(
    con: sqlite3.Connection,
    table_name: str,
    column_name: str,
    column_type: str,
) -> None:
    existing = get_existing_columns(con, table_name)
    if column_name in existing:
        return

    add_type = _sanitize_add_column_type(column_type)

    try:
        con.execute(
            f"""
            ALTER TABLE {quote_ident(table_name)}
            ADD COLUMN {quote_ident(column_name)} {add_type}
            """
        )
        logger.warning(
            "[RANKING SNAPSHOT SCHEMA] added missing column table=%s column=%s type=%s",
            table_name,
            column_name,
            add_type,
        )
    except sqlite3.OperationalError as exc:
        msg = str(exc).lower()
        if "duplicate column" in msg:
            return

        logger.exception(
            "[RANKING SNAPSHOT SCHEMA] add column failed table=%s column=%s type=%s",
            table_name,
            column_name,
            add_type,
        )
        raise


def _ensure_required_columns(con: sqlite3.Connection) -> int:
    existing = get_existing_columns(con, SNAPSHOT_TABLE)
    patched = 0

    for col, typ in RANKING_SNAPSHOT_COLUMNS:
        if col not in existing:
            ensure_column(con, SNAPSHOT_TABLE, col, typ)
            existing.add(col)
            patched += 1

    if patched:
        logger.warning(
            "[RANKING SNAPSHOT SCHEMA] schema patch completed table=%s patched_columns=%s",
            SNAPSHOT_TABLE,
            patched,
        )

    return patched


def _safe_update(con: sqlite3.Connection, sql: str) -> None:
    try:
        con.execute(sql)
    except Exception:
        logger.debug(
            "[RANKING SNAPSHOT SCHEMA] optional backfill skipped sql=%s",
            sql,
            exc_info=True,
        )


def _backfill_alias_columns(con: sqlite3.Connection) -> None:
    """
    既存DBのカラム揺れを補完する。
    """
    cols = get_existing_columns(con, SNAPSHOT_TABLE)

    if {"ranking_type", "rank_type"}.issubset(cols):
        _safe_update(
            con,
            f"""
            UPDATE {quote_ident(SNAPSHOT_TABLE)}
               SET ranking_type = rank_type
             WHERE (ranking_type IS NULL OR ranking_type = '')
               AND rank_type IS NOT NULL
               AND rank_type != ''
            """,
        )

    if {"rank_type", "ranking_type"}.issubset(cols):
        _safe_update(
            con,
            f"""
            UPDATE {quote_ident(SNAPSHOT_TABLE)}
               SET rank_type = ranking_type
             WHERE (rank_type IS NULL OR rank_type = '')
               AND ranking_type IS NOT NULL
               AND ranking_type != ''
            """,
        )

    if {"snapshot_time", "datetime"}.issubset(cols):
        _safe_update(
            con,
            f"""
            UPDATE {quote_ident(SNAPSHOT_TABLE)}
               SET snapshot_time = datetime
             WHERE (snapshot_time IS NULL OR snapshot_time = '')
               AND datetime IS NOT NULL
               AND datetime != ''
            """,
        )

    if {"price", "current_price"}.issubset(cols):
        _safe_update(
            con,
            f"""
            UPDATE {quote_ident(SNAPSHOT_TABLE)}
               SET price = current_price
             WHERE (price IS NULL OR price = 0)
               AND current_price IS NOT NULL
               AND current_price > 0
            """,
        )

    if {"current_price", "price"}.issubset(cols):
        _safe_update(
            con,
            f"""
            UPDATE {quote_ident(SNAPSHOT_TABLE)}
               SET current_price = price
             WHERE (current_price IS NULL OR current_price = 0)
               AND price IS NOT NULL
               AND price > 0
            """,
        )


def ensure_ranking_snapshot_table(con: sqlite3.Connection) -> None:
    columns_sql = ",\n".join(
        f"{quote_ident(col)} {typ}" for col, typ in RANKING_SNAPSHOT_COLUMNS
    )

    con.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {quote_ident(SNAPSHOT_TABLE)} (
            {columns_sql}
        )
        """
    )

    _ensure_required_columns(con)
    _backfill_alias_columns(con)


def patch_ranking_snapshot_schema(con: sqlite3.Connection) -> int:
    if not _table_exists(con, SNAPSHOT_TABLE):
        ensure_ranking_snapshot_table(con)
        return 0

    patched = _ensure_required_columns(con)
    _backfill_alias_columns(con)
    return patched


def ensure_ranking_snapshot_unique_index(con: sqlite3.Connection) -> None:
    patch_ranking_snapshot_schema(con)

    try:
        con.execute(
            f"""
            CREATE UNIQUE INDEX IF NOT EXISTS {quote_ident(SNAPSHOT_UNIQUE_INDEX)}
            ON {quote_ident(SNAPSHOT_TABLE)} (
                symbol,
                datetime,
                ranking_type,
                market
            )
            """
        )
    except sqlite3.OperationalError:
        logger.exception(
            "[RANKING SNAPSHOT SCHEMA] create unique index failed index=%s",
            SNAPSHOT_UNIQUE_INDEX,
        )
        raise


__all__ = [
    "SNAPSHOT_TABLE",
    "SNAPSHOT_UNIQUE_INDEX",
    "SNAPSHOT_UNIQUE_INDEX_SNAPSHOT_TIME",
    "RANKING_SNAPSHOT_COLUMNS",
    "get_existing_columns",
    "ensure_ranking_snapshot_table",
    "patch_ranking_snapshot_schema",
    "ensure_ranking_snapshot_unique_index",
]
