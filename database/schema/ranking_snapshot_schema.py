# ============================================================
# File   : database/schema/ranking_snapshot_schema.py
# Version: PRODUCTION-STABLE-REV2.1-RANKING-SNAPSHOT-SCHEMA-FIX-MISSING-RANKING-TYPE-NO-INTERNAL-COMMIT
# ------------------------------------------------------------
# 【概要】
#   ranking_snapshot_1min schema 管理。
#
# 【修正内容】
#   - 既存 ranking_snapshot_1min に ranking_type が無い場合でも落ちない
#   - CREATE TABLE IF NOT EXISTS 後、必ず不足カラムを ALTER TABLE で追加
#   - index / unique index 作成前に必要カラムを保証
#   - datetime / snapshot_time / ranking_type / rank_type / market の揺れを補完
#   - no such column: ranking_type を防止
#   - このモジュール内では con.commit() しない
#     → COMMIT / ROLLBACK は呼び出し元 migrate_ranking.py に任せる
#   - cannot commit - no transaction is active を防止
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

    注意:
      - PRIMARY KEY は後付けできない
      - DEFAULT CURRENT_TIMESTAMP は ADD COLUMN では制限に当たることがある
      - NOT NULL かつ DEFAULT なしは既存行があると失敗する
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
               AND rank_type <> ''
            """,
        )
        _safe_update(
            con,
            f"""
            UPDATE {quote_ident(SNAPSHOT_TABLE)}
               SET rank_type = ranking_type
             WHERE (rank_type IS NULL OR rank_type = '')
               AND ranking_type IS NOT NULL
               AND ranking_type <> ''
            """,
        )

    if {"datetime", "snapshot_time"}.issubset(cols):
        _safe_update(
            con,
            f"""
            UPDATE {quote_ident(SNAPSHOT_TABLE)}
               SET datetime = snapshot_time
             WHERE (datetime IS NULL OR datetime = '')
               AND snapshot_time IS NOT NULL
               AND snapshot_time <> ''
            """,
        )
        _safe_update(
            con,
            f"""
            UPDATE {quote_ident(SNAPSHOT_TABLE)}
               SET snapshot_time = datetime
             WHERE (snapshot_time IS NULL OR snapshot_time = '')
               AND datetime IS NOT NULL
               AND datetime <> ''
            """,
        )

    if {"current_price", "price"}.issubset(cols):
        _safe_update(
            con,
            f"""
            UPDATE {quote_ident(SNAPSHOT_TABLE)}
               SET price = current_price
             WHERE price IS NULL
               AND current_price IS NOT NULL
            """,
        )
        _safe_update(
            con,
            f"""
            UPDATE {quote_ident(SNAPSHOT_TABLE)}
               SET current_price = price
             WHERE current_price IS NULL
               AND price IS NOT NULL
            """,
        )

    if "market" in cols:
        _safe_update(
            con,
            f"""
            UPDATE {quote_ident(SNAPSHOT_TABLE)}
               SET market = ''
             WHERE market IS NULL
            """,
        )

    if "ranking_type" in cols:
        _safe_update(
            con,
            f"""
            UPDATE {quote_ident(SNAPSHOT_TABLE)}
               SET ranking_type = ''
             WHERE ranking_type IS NULL
            """,
        )


def _columns_exist(
    con: sqlite3.Connection,
    table_name: str,
    columns: list[str],
) -> tuple[bool, list[str]]:
    existing = get_existing_columns(con, table_name)
    missing = [c for c in columns if c not in existing]
    return len(missing) == 0, missing


def _safe_create_index(
    con: sqlite3.Connection,
    *,
    index_name: str,
    columns: list[str],
    unique: bool = False,
) -> bool:
    ok, missing = _columns_exist(con, SNAPSHOT_TABLE, columns)
    if not ok:
        logger.warning(
            "[RANKING SNAPSHOT SCHEMA] skip index because columns missing index=%s missing=%s",
            index_name,
            missing,
        )
        return False

    unique_sql = "UNIQUE " if unique else ""
    col_sql = ", ".join(quote_ident(c) for c in columns)

    try:
        con.execute(
            f"""
            CREATE {unique_sql}INDEX IF NOT EXISTS {quote_ident(index_name)}
            ON {quote_ident(SNAPSHOT_TABLE)}({col_sql})
            """
        )
        logger.info(
            "[RANKING SNAPSHOT SCHEMA] index ensured index=%s unique=%s columns=%s",
            index_name,
            unique,
            columns,
        )
        return True
    except sqlite3.OperationalError:
        logger.exception(
            "[RANKING SNAPSHOT SCHEMA] index create failed index=%s columns=%s",
            index_name,
            columns,
        )
        raise


def ensure_ranking_snapshot_table(con: sqlite3.Connection) -> None:
    """
    ranking_snapshot_1min のテーブルと基本indexを保証する。

    重要:
      既存テーブルに対して CREATE TABLE IF NOT EXISTS は不足カラムを追加しない。
      そのため、index 作成前に必ず _ensure_required_columns() を呼ぶ。

    注意:
      この関数内では commit しない。
      commit / rollback は呼び出し元 migrate_ranking.py に任せる。
    """
    con.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {quote_ident(SNAPSHOT_TABLE)} (
            symbol TEXT NOT NULL,
            datetime TEXT NOT NULL,
            snapshot_time TEXT NOT NULL,
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
            ranking_type TEXT NOT NULL DEFAULT '',
            rank_type TEXT,
            rank_type_id INTEGER,
            category TEXT,
            market TEXT NOT NULL DEFAULT '',
            exchange TEXT,
            source TEXT,
            rank INTEGER,
            rank_position INTEGER,
            best_rank INTEGER,
            price_delta_1m REAL,
            volume_delta_1m REAL,
            volume_speed REAL,
            minute_of_day INTEGER,
            date TEXT,
            time TEXT,
            raw_json TEXT,
            ingest_id TEXT,
            received_at TEXT,
            created_at TEXT,
            inserted_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (symbol, datetime, ranking_type, market)
        )
        """
    )

    if not _table_exists(con, SNAPSHOT_TABLE):
        raise RuntimeError(f"failed to create table: {SNAPSHOT_TABLE}")

    _ensure_required_columns(con)
    _backfill_alias_columns(con)

    _safe_create_index(
        con,
        index_name="idx_ranking_snapshot_1min_dt",
        columns=["datetime"],
    )
    _safe_create_index(
        con,
        index_name="idx_ranking_snapshot_1min_snapshot_time",
        columns=["snapshot_time"],
    )
    _safe_create_index(
        con,
        index_name="idx_ranking_snapshot_1min_symbol_dt",
        columns=["symbol", "datetime"],
    )
    _safe_create_index(
        con,
        index_name="idx_ranking_snapshot_1min_symbol_snapshot_time",
        columns=["symbol", "snapshot_time"],
    )
    _safe_create_index(
        con,
        index_name="idx_ranking_snapshot_1min_type_market_dt",
        columns=["ranking_type", "market", "datetime"],
    )
    _safe_create_index(
        con,
        index_name="idx_ranking_snapshot_1min_type_market_snapshot_time",
        columns=["ranking_type", "market", "snapshot_time"],
    )

    # ここでは commit しない。
    # commit / rollback は呼び出し元 migrate_ranking.py に任せる。


def patch_ranking_snapshot_schema(con: sqlite3.Connection) -> None:
    """
    互換API。
    既存呼び出し元が patch_ranking_snapshot_schema を呼んでも、
    必ず安全にテーブル/カラム/indexまで保証する。
    """
    ensure_ranking_snapshot_table(con)


def index_exists(con: sqlite3.Connection, index_name: str) -> bool:
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


def dedupe_existing_snapshot_rows_for_unique_index(con: sqlite3.Connection) -> int:
    """
    UNIQUE INDEX 作成前に重複行を削除する。

    既存DBで ranking_type / market / datetime が NULL の場合があるため、
    COALESCE で同一判定する。
    """
    ensure_ranking_snapshot_table(con)

    before_row = con.execute(
        f"SELECT COUNT(*) FROM {quote_ident(SNAPSHOT_TABLE)}"
    ).fetchone()
    before = int(before_row[0] or 0)

    con.execute(
        f"""
        DELETE FROM {quote_ident(SNAPSHOT_TABLE)}
         WHERE rowid NOT IN (
                SELECT MAX(rowid)
                  FROM {quote_ident(SNAPSHOT_TABLE)}
                 GROUP BY
                       symbol,
                       COALESCE(datetime, ''),
                       COALESCE(ranking_type, ''),
                       COALESCE(market, '')
         )
        """
    )

    after_row = con.execute(
        f"SELECT COUNT(*) FROM {quote_ident(SNAPSHOT_TABLE)}"
    ).fetchone()
    after = int(after_row[0] or 0)

    deleted = before - after

    if deleted > 0:
        logger.warning(
            "[RANKING SNAPSHOT SCHEMA] duplicate rows removed before unique index deleted=%s before=%s after=%s",
            deleted,
            before,
            after,
        )

    return deleted


def _dedupe_existing_snapshot_rows_for_snapshot_time_unique_index(
    con: sqlite3.Connection,
) -> int:
    ensure_ranking_snapshot_table(con)

    before_row = con.execute(
        f"SELECT COUNT(*) FROM {quote_ident(SNAPSHOT_TABLE)}"
    ).fetchone()
    before = int(before_row[0] or 0)

    con.execute(
        f"""
        DELETE FROM {quote_ident(SNAPSHOT_TABLE)}
         WHERE rowid NOT IN (
                SELECT MAX(rowid)
                  FROM {quote_ident(SNAPSHOT_TABLE)}
                 GROUP BY
                       symbol,
                       COALESCE(snapshot_time, ''),
                       COALESCE(ranking_type, ''),
                       COALESCE(market, '')
         )
        """
    )

    after_row = con.execute(
        f"SELECT COUNT(*) FROM {quote_ident(SNAPSHOT_TABLE)}"
    ).fetchone()
    after = int(after_row[0] or 0)

    deleted = before - after

    if deleted > 0:
        logger.warning(
            "[RANKING SNAPSHOT SCHEMA] duplicate rows removed before snapshot_time unique index deleted=%s before=%s after=%s",
            deleted,
            before,
            after,
        )

    return deleted


def ensure_ranking_snapshot_unique_index(con: sqlite3.Connection) -> None:
    """
    ranking_snapshot_1min の UNIQUE INDEX を保証する。

    今回のエラー対策:
      - UNIQUE INDEX 作成前に ensure_ranking_snapshot_table を呼ぶ
      - ranking_type が無い既存DBでも先に ALTER TABLE で追加する

    注意:
      この関数内では commit しない。
      commit / rollback は呼び出し元 migrate_ranking.py に任せる。
    """
    ensure_ranking_snapshot_table(con)

    if not index_exists(con, SNAPSHOT_UNIQUE_INDEX):
        dedupe_existing_snapshot_rows_for_unique_index(con)

        _safe_create_index(
            con,
            index_name=SNAPSHOT_UNIQUE_INDEX,
            columns=["symbol", "datetime", "ranking_type", "market"],
            unique=True,
        )

        logger.warning(
            "[RANKING SNAPSHOT SCHEMA] unique index ensured index=%s columns=symbol,datetime,ranking_type,market",
            SNAPSHOT_UNIQUE_INDEX,
        )

    # datetime が空で snapshot_time が正本になるDBもあるため、補助的にこちらも作る。
    # ただし、既存の呼び出し側との互換のため、失敗した場合は警告に留める。
    try:
        if not index_exists(con, SNAPSHOT_UNIQUE_INDEX_SNAPSHOT_TIME):
            _dedupe_existing_snapshot_rows_for_snapshot_time_unique_index(con)

            _safe_create_index(
                con,
                index_name=SNAPSHOT_UNIQUE_INDEX_SNAPSHOT_TIME,
                columns=["symbol", "snapshot_time", "ranking_type", "market"],
                unique=True,
            )

            logger.warning(
                "[RANKING SNAPSHOT SCHEMA] unique index ensured index=%s columns=symbol,snapshot_time,ranking_type,market",
                SNAPSHOT_UNIQUE_INDEX_SNAPSHOT_TIME,
            )
    except Exception:
        logger.warning(
            "[RANKING SNAPSHOT SCHEMA] snapshot_time unique index skipped",
            exc_info=True,
        )

    # ここでは commit しない。
    # commit / rollback は呼び出し元 migrate_ranking.py に任せる。


def inspect_ranking_snapshot_schema(con: sqlite3.Connection) -> dict[str, Any]:
    """
    デバッグ用。migration 側から呼んでも安全。
    """
    exists = _table_exists(con, SNAPSHOT_TABLE)
    columns = sorted(get_existing_columns(con, SNAPSHOT_TABLE)) if exists else []

    indexes = con.execute(
        """
        SELECT name, sql
          FROM sqlite_master
         WHERE type='index'
           AND tbl_name=?
         ORDER BY name
        """,
        (SNAPSHOT_TABLE,),
    ).fetchall()

    row_count = 0
    if exists:
        try:
            row = con.execute(
                f"SELECT COUNT(*) FROM {quote_ident(SNAPSHOT_TABLE)}"
            ).fetchone()
            row_count = int(row[0] or 0)
        except Exception:
            row_count = -1

    return {
        "table": SNAPSHOT_TABLE,
        "exists": exists,
        "columns": columns,
        "indexes": [{"name": r[0], "sql": r[1]} for r in indexes],
        "row_count": row_count,
        "ok": exists and "ranking_type" in columns,
    }


__all__ = [
    "SNAPSHOT_TABLE",
    "SNAPSHOT_UNIQUE_INDEX",
    "SNAPSHOT_UNIQUE_INDEX_SNAPSHOT_TIME",
    "RANKING_SNAPSHOT_COLUMNS",
    "ensure_ranking_snapshot_table",
    "get_existing_columns",
    "ensure_column",
    "patch_ranking_snapshot_schema",
    "index_exists",
    "dedupe_existing_snapshot_rows_for_unique_index",
    "ensure_ranking_snapshot_unique_index",
    "inspect_ranking_snapshot_schema",
]