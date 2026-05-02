# ============================================================
# File   : trading/ranking/summary/persistence/schema.py
# Ver    : PRODUCTION-STABLE-REV3.1-USE-DATABASE-SQLITE
# ------------------------------------------------------------
# 【概要】
#   ranking_summary_1min / 3min / 5min の schema 管理。
#
# 【責務】
#   - table name 解決
#   - CREATE TABLE
#   - ALTER TABLE ADD COLUMN
#   - UNIQUE INDEX 作成
#   - null key cleanup
#   - duplicate cleanup
#
# REV3.1:
#   ✔ database.sqlite 不使用
#   ✔ 既存 database/sqlite/__init__.py の関数を使用
#   ✔ is_sqlite_locked_error → is_lock_error に統一
#   ✔ quote_ident / prepare_sqlite_connection を既存共通部品から使用
# ============================================================

from __future__ import annotations

import logging
import sqlite3
import threading
from typing import Dict, Any, List, Optional

from .paths import DEFAULT_RANKING_DIR, get_ranking_db_path

from database.sqlite import (
    DEFAULT_BUSY_TIMEOUT_MS,
    is_lock_error,
    lock_sleep_seconds,
    prepare_sqlite_connection,
    quote_ident,
)

logger = logging.getLogger(__name__)

RANKING_SUMMARY_DB_LOCK = threading.RLock()

MAX_SAVE_RETRY = 6


# ============================================================
# schema columns
# ============================================================

RANKING_SUMMARY_COLUMNS: List[tuple[str, str]] = [
    ("symbol", "TEXT NOT NULL"),
    ("symbolname", "TEXT"),
    ("datetime", "TEXT NOT NULL"),
    ("date", "TEXT"),
    ("time", "TEXT"),
    ("time_range", "TEXT"),

    ("open", "REAL"),
    ("high", "REAL"),
    ("low", "REAL"),
    ("close", "REAL"),
    ("volume", "REAL DEFAULT 0"),

    ("open_price", "REAL"),
    ("high_price", "REAL"),
    ("low_price", "REAL"),
    ("close_price", "REAL"),
    ("current_price", "REAL"),

    ("ranking_type", "TEXT"),
    ("rank", "REAL"),
    ("best_rank", "REAL"),
    ("hit_count", "REAL"),
    ("hist", "REAL"),
    ("change_percentage", "REAL"),
    ("trading_volume", "REAL"),
    ("trading_value", "REAL"),
    ("turnover", "REAL"),
    ("tick_count", "REAL"),

    ("ma5", "REAL"),
    ("ma25", "REAL"),
    ("ma75", "REAL"),
    ("rsi", "REAL"),
    ("rsi_slope", "REAL"),
    ("macd", "REAL"),
    ("signal", "REAL"),
    ("macd_signal", "REAL"),
    ("macd_hist", "REAL"),
    ("macd_hist_slope", "REAL"),
    ("slope", "REAL"),
    ("slope_atr_scaled", "REAL"),

    ("price_delta", "REAL"),
    ("price_delta_pct", "REAL"),
    ("ranking_atr_proxy", "REAL"),
    ("ranking_momentum", "REAL"),
    ("rank_improve", "REAL"),
    ("volume_delta", "REAL"),
    ("ranking_score", "REAL"),

    ("mtf", "REAL"),
    ("score_mtf", "REAL"),
    ("mtf_score", "REAL"),

    ("flag_macd_cross", "INTEGER"),
    ("flag_macd_hist_expand", "INTEGER"),
    ("flag_rsi_rebound", "INTEGER"),
    ("flag_rsi_midline_cross", "INTEGER"),
    ("flag_macd_dc", "INTEGER"),
    ("flag_macd_hist_contract", "INTEGER"),
    ("flag_rsi_falling", "INTEGER"),
    ("flag_rsi_overbought_70", "INTEGER"),

    ("score", "REAL"),
    ("score_buy", "REAL"),
    ("score_sell", "REAL"),
    ("score_total", "REAL"),
    ("final_score", "REAL"),
    ("display_score", "REAL"),
    ("disp_score", "REAL"),
    ("score_slope", "REAL"),

    ("base", "REAL"),
    ("trend", "REAL"),
    ("mom", "REAL"),
    ("vel", "REAL"),
    ("pen", "REAL"),

    ("interval", "INTEGER"),
    ("source", "TEXT"),
    ("price_source", "TEXT"),
    ("mode", "TEXT"),
    ("updated_at", "TEXT"),
]


# ============================================================
# connection helpers
# ============================================================

def _connect(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(
        path,
        timeout=max(10.0, float(DEFAULT_BUSY_TIMEOUT_MS) / 1000.0),
        check_same_thread=False,
        isolation_level=None,
    )
    prepare_sqlite_connection(con)
    return con


def _begin_immediate(con: sqlite3.Connection) -> None:
    con.execute("BEGIN IMMEDIATE")


def _commit(con: sqlite3.Connection) -> None:
    con.execute("COMMIT")


def _rollback_quietly(con: Optional[sqlite3.Connection]) -> None:
    if con is None:
        return
    try:
        con.execute("ROLLBACK")
    except Exception:
        pass


def _close_quietly(con: Optional[sqlite3.Connection]) -> None:
    if con is None:
        return
    try:
        con.close()
    except Exception:
        pass


# ============================================================
# schema helpers
# ============================================================

def table_name(interval: int) -> str:
    interval = int(interval)
    if interval not in (1, 3, 5):
        raise ValueError(f"unsupported interval: {interval}")
    return f"ranking_summary_{interval}min"


def column_type_map() -> Dict[str, str]:
    return dict(RANKING_SUMMARY_COLUMNS)


def create_table_sql(table: str) -> str:
    col_lines = []
    for name, typ in RANKING_SUMMARY_COLUMNS:
        col_lines.append(f"            {quote_ident(name)} {typ}")

    cols_sql = ",\n".join(col_lines)

    return f"""
        CREATE TABLE IF NOT EXISTS {quote_ident(table)} (
{cols_sql},

            PRIMARY KEY (symbol, datetime)
        )
    """


def table_exists(con: sqlite3.Connection, table: str) -> bool:
    try:
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
    except Exception:
        logger.exception("[RANKING SUMMARY TABLE] table_exists failed table=%s", table)
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
        logger.exception("[RANKING SUMMARY TABLE] index_exists failed index=%s", index_name)
        return False


def get_existing_columns(con: sqlite3.Connection, table: str) -> Dict[str, Dict[str, Any]]:
    cols: Dict[str, Dict[str, Any]] = {}
    try:
        if not table_exists(con, table):
            return cols

        rows = con.execute(f"PRAGMA table_info({quote_ident(table)})").fetchall()
        for row in rows:
            cols[str(row[1])] = {
                "cid": row[0],
                "name": row[1],
                "type": row[2],
                "notnull": row[3],
                "dflt_value": row[4],
                "pk": row[5],
            }
    except Exception:
        logger.warning(
            "[RANKING SUMMARY TABLE] get columns failed table=%s",
            table,
            exc_info=True,
        )
    return cols


def dedupe_ranking_summary_table(con: sqlite3.Connection, table: str) -> int:
    try:
        if not table_exists(con, table):
            return 0

        before = con.execute(f"SELECT COUNT(*) FROM {quote_ident(table)}").fetchone()[0]

        dup_count = con.execute(
            f"""
            SELECT COUNT(*)
              FROM (
                    SELECT symbol, datetime, COUNT(*) AS cnt
                      FROM {quote_ident(table)}
                     WHERE symbol IS NOT NULL
                       AND datetime IS NOT NULL
                       AND TRIM(CAST(symbol AS TEXT)) <> ''
                       AND TRIM(CAST(datetime AS TEXT)) <> ''
                     GROUP BY symbol, datetime
                    HAVING COUNT(*) > 1
                   )
            """
        ).fetchone()[0]

        if int(dup_count) <= 0:
            return 0

        con.execute(
            f"""
            DELETE FROM {quote_ident(table)}
             WHERE rowid NOT IN (
                    SELECT MAX(rowid)
                      FROM {quote_ident(table)}
                     WHERE symbol IS NOT NULL
                       AND datetime IS NOT NULL
                       AND TRIM(CAST(symbol AS TEXT)) <> ''
                       AND TRIM(CAST(datetime AS TEXT)) <> ''
                     GROUP BY symbol, datetime
             )
               AND symbol IS NOT NULL
               AND datetime IS NOT NULL
               AND TRIM(CAST(symbol AS TEXT)) <> ''
               AND TRIM(CAST(datetime AS TEXT)) <> ''
            """
        )

        after = con.execute(f"SELECT COUNT(*) FROM {quote_ident(table)}").fetchone()[0]
        deleted = int(before) - int(after)

        if deleted > 0:
            logger.warning(
                "[RANKING SUMMARY TABLE] dedupe done table=%s duplicate_keys=%s before=%s after=%s deleted=%s",
                table,
                dup_count,
                before,
                after,
                deleted,
            )
        return deleted

    except Exception:
        logger.exception("[RANKING SUMMARY TABLE] dedupe failed table=%s", table)
        return 0


def delete_null_key_rows(con: sqlite3.Connection, table: str) -> int:
    try:
        if not table_exists(con, table):
            return 0

        before = con.execute(f"SELECT COUNT(*) FROM {quote_ident(table)}").fetchone()[0]

        con.execute(
            f"""
            DELETE FROM {quote_ident(table)}
             WHERE symbol IS NULL
                OR datetime IS NULL
                OR TRIM(CAST(symbol AS TEXT)) = ''
                OR TRIM(CAST(datetime AS TEXT)) = ''
            """
        )

        after = con.execute(f"SELECT COUNT(*) FROM {quote_ident(table)}").fetchone()[0]
        deleted = int(before) - int(after)

        if deleted > 0:
            logger.warning(
                "[RANKING SUMMARY TABLE] null-key rows deleted table=%s before=%s after=%s deleted=%s",
                table,
                before,
                after,
                deleted,
            )

        return deleted

    except Exception:
        logger.warning(
            "[RANKING SUMMARY TABLE] null-key cleanup skipped table=%s",
            table,
            exc_info=True,
        )
        return 0


def add_missing_columns(con: sqlite3.Connection, table: str) -> None:
    if not table_exists(con, table):
        return

    existing = get_existing_columns(con, table)
    expected = column_type_map()

    added = 0
    for col, typ in expected.items():
        if col in existing:
            continue

        add_typ = typ
        if "NOT NULL" in add_typ.upper() and "DEFAULT" not in add_typ.upper():
            add_typ = add_typ.upper().replace(" NOT NULL", "")

        try:
            con.execute(
                f"""
                ALTER TABLE {quote_ident(table)}
                ADD COLUMN {quote_ident(col)} {add_typ}
                """
            )
            added += 1
            logger.warning(
                "[RANKING SUMMARY TABLE] added missing column table=%s column=%s type=%s",
                table,
                col,
                add_typ,
            )
        except sqlite3.OperationalError as e:
            if "duplicate column" in str(e).lower():
                continue
            logger.warning(
                "[RANKING SUMMARY TABLE] add column failed table=%s column=%s err=%s",
                table,
                col,
                e,
                exc_info=True,
            )

    if added > 0:
        logger.warning(
            "[RANKING SUMMARY TABLE] schema upgraded table=%s added_columns=%s",
            table,
            added,
        )


def ensure_ranking_summary_table(
    con: sqlite3.Connection,
    *,
    interval: int,
) -> None:
    table = table_name(interval)

    con.execute(create_table_sql(table))
    add_missing_columns(con, table)

    delete_null_key_rows(con, table)
    dedupe_ranking_summary_table(con, table)

    index_name = f"uq_{table}_symbol_datetime"

    if not index_exists(con, index_name):
        try:
            con.execute(
                f"""
                CREATE UNIQUE INDEX IF NOT EXISTS
                    {quote_ident(index_name)}
                ON {quote_ident(table)}(symbol, datetime)
                """
            )
            logger.info(
                "[RANKING SUMMARY TABLE] unique index ensured table=%s index=%s",
                table,
                index_name,
            )
        except sqlite3.IntegrityError:
            logger.warning(
                "[RANKING SUMMARY TABLE] unique index retry after dedupe table=%s",
                table,
                exc_info=True,
            )
            delete_null_key_rows(con, table)
            dedupe_ranking_summary_table(con, table)
            con.execute(
                f"""
                CREATE UNIQUE INDEX IF NOT EXISTS
                    {quote_ident(index_name)}
                ON {quote_ident(table)}(symbol, datetime)
                """
            )
            logger.info(
                "[RANKING SUMMARY TABLE] unique index ensured after retry table=%s index=%s",
                table,
                index_name,
            )

    con.execute(
        f"""
        CREATE INDEX IF NOT EXISTS
            {quote_ident(f"idx_{table}_datetime")}
        ON {quote_ident(table)}(datetime)
        """
    )

    con.execute(
        f"""
        CREATE INDEX IF NOT EXISTS
            {quote_ident(f"idx_{table}_score")}
        ON {quote_ident(table)}(display_score)
        """
    )

    con.execute(
        f"""
        CREATE INDEX IF NOT EXISTS
            {quote_ident(f"idx_{table}_symbol_datetime")}
        ON {quote_ident(table)}(symbol, datetime)
        """
    )

    con.execute(
        f"""
        CREATE INDEX IF NOT EXISTS
            {quote_ident(f"idx_{table}_ranking_type")}
        ON {quote_ident(table)}(ranking_type)
        """
    )


def ensure_all_ranking_summary_tables(
    *,
    trade_date=None,
    db_path: Optional[str] = None,
    ranking_dir: str = DEFAULT_RANKING_DIR,
) -> None:
    path = db_path or get_ranking_db_path(trade_date, ranking_dir=ranking_dir)

    last_err = None

    for attempt in range(1, MAX_SAVE_RETRY + 1):
        con: Optional[sqlite3.Connection] = None

        try:
            with RANKING_SUMMARY_DB_LOCK:
                con = _connect(path)
                _begin_immediate(con)

                for interval in (1, 3, 5):
                    ensure_ranking_summary_table(con, interval=interval)

                _commit(con)
                _close_quietly(con)
                con = None

            logger.info(
                "[RANKING SUMMARY TABLE] all tables ensured path=%s attempt=%s",
                path,
                attempt,
            )
            return

        except sqlite3.OperationalError as e:
            last_err = e
            _rollback_quietly(con)

            if is_lock_error(e) and attempt < MAX_SAVE_RETRY:
                sleep_sec = lock_sleep_seconds(attempt)
                logger.warning(
                    "[RANKING SUMMARY TABLE] ensure_all locked retry path=%s attempt=%s/%s sleep=%.2fs err=%s",
                    path,
                    attempt,
                    MAX_SAVE_RETRY,
                    sleep_sec,
                    e,
                )
                continue

            logger.exception("[RANKING SUMMARY TABLE] ensure_all failed path=%s", path)

        except Exception as e:
            last_err = e
            _rollback_quietly(con)
            logger.exception("[RANKING SUMMARY TABLE] ensure_all failed path=%s", path)

        finally:
            _close_quietly(con)

        break

    logger.error(
        "[RANKING SUMMARY TABLE] ensure_all failed after retries path=%s last_err=%s",
        path,
        last_err,
    )


__all__ = [
    "RANKING_SUMMARY_DB_LOCK",
    "RANKING_SUMMARY_COLUMNS",
    "table_name",
    "column_type_map",
    "create_table_sql",
    "table_exists",
    "index_exists",
    "get_existing_columns",
    "dedupe_ranking_summary_table",
    "delete_null_key_rows",
    "add_missing_columns",
    "ensure_ranking_summary_table",
    "ensure_all_ranking_summary_tables",
]