# ============================================================
# migrate_optional_db.py
# ------------------------------------------------------------
# ・optional_data.db のマイグレーション
# ・既存 DB を壊さず不足テーブル／カラムのみ追加
# ・何度実行しても安全
# ・paths.py 前提（Y:/ 直書き禁止）
# ============================================================

import sqlite3
from pathlib import Path
import logging

from config.paths import get_path

logger = logging.getLogger(__name__)

# ------------------------------------------------------------
# paths.py 経由
# ------------------------------------------------------------
DB_OPTIONAL: Path = get_path("optional_db")


# ------------------------------------------------------------
# helper
# ------------------------------------------------------------
def ensure_table(cur: sqlite3.Cursor, create_sql: str, table_name: str):
    try:
        cur.execute(create_sql)
        logger.info(f"✔ ensure table: {table_name}")
    except Exception as e:
        logger.warning(f"⚠ CREATE TABLE failed [{table_name}]: {e}")


def ensure_column(
    cur: sqlite3.Cursor,
    table: str,
    column: str,
    col_type: str,
):
    """
    column が無ければ ALTER TABLE ADD COLUMN
    """
    try:
        cur.execute(f"PRAGMA table_info({table})")
        cols = [r[1] for r in cur.fetchall()]

        if column not in cols:
            logger.info(f"➕ add column: {table}.{column} ({col_type})")
            cur.execute(
                f"ALTER TABLE {table} ADD COLUMN {column} {col_type}"
            )
    except Exception as e:
        logger.warning(
            f"⚠ ALTER failed [{table}.{column}]: {e}"
        )


# ------------------------------------------------------------
# main
# ------------------------------------------------------------
def migrate_optional_db(db_path: Path | None = None):

    db_path = Path(db_path) if db_path else DB_OPTIONAL

    if not db_path.exists():
        logger.error(f"❌ optional DB not found: {db_path}")
        return

    logger.info("=" * 60)
    logger.info("⏳ migrate_optional_db START")
    logger.info(f" DB = {db_path}")
    logger.info("=" * 60)

    with sqlite3.connect(db_path) as conn:
        cur = conn.cursor()

        # ----------------------------------------------------
        # news_events
        # ----------------------------------------------------
        ensure_table(
            cur,
            """
            CREATE TABLE IF NOT EXISTS news_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT,
                symbolname TEXT,
                category TEXT,
                title TEXT,
                comment TEXT,
                date TEXT,
                created_at TEXT
            )
            """,
            "news_events",
        )

        ensure_column(cur, "news_events", "symbolname", "TEXT")
        ensure_column(cur, "news_events", "comment", "TEXT")
        ensure_column(cur, "news_events", "created_at", "TEXT")

        # ----------------------------------------------------
        # pts_rank
        # ----------------------------------------------------
        ensure_table(
            cur,
            """
            CREATE TABLE IF NOT EXISTS pts_rank (
                symbol TEXT,
                pts_diff REAL,
                date TEXT
            )
            """,
            "pts_rank",
        )

        ensure_column(cur, "pts_rank", "pts_diff", "REAL")

        # ----------------------------------------------------
        # daily_watchlist
        # ----------------------------------------------------
        ensure_table(
            cur,
            """
            CREATE TABLE IF NOT EXISTS daily_watchlist (
                symbol TEXT,
                symbolname TEXT,
                date TEXT,
                buy_score INTEGER,
                sell_score INTEGER,
                reason_buy TEXT,
                reason_sell TEXT,
                PRIMARY KEY (symbol, date)
            )
            """,
            "daily_watchlist",
        )

        ensure_column(cur, "daily_watchlist", "symbolname", "TEXT")
        ensure_column(cur, "daily_watchlist", "buy_score", "INTEGER")
        ensure_column(cur, "daily_watchlist", "sell_score", "INTEGER")
        ensure_column(cur, "daily_watchlist", "reason_buy", "TEXT")
        ensure_column(cur, "daily_watchlist", "reason_sell", "TEXT")

        # ----------------------------------------------------
        # margin_master
        # ----------------------------------------------------
        ensure_table(
            cur,
            """
            CREATE TABLE IF NOT EXISTS margin_master (
                symbol TEXT PRIMARY KEY,
                margin_status TEXT,
                note TEXT,
                updated_at TEXT
            )
            """,
            "margin_master",
        )

        ensure_column(cur, "margin_master", "margin_status", "TEXT")
        ensure_column(cur, "margin_master", "note", "TEXT")
        ensure_column(cur, "margin_master", "updated_at", "TEXT")

        conn.commit()

    logger.info("=" * 60)
    logger.info("🎉 optional DB migration DONE")
    logger.info("=" * 60)


# ------------------------------------------------------------
# entry point
# ------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    migrate_optional_db()
