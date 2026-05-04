# ============================================================
# optional/db/migrate.py
# ------------------------------------------------------------
# ✔ OPTIONAL 系 DB マイグレーション（本番用）
# ✔ ADD ONLY / 冪等 / SAFE
# ✔ database is locked 耐性
# ✔ runtime と分離して単独実行可能
# ============================================================

import logging
from pathlib import Path

from config.paths import get_path
from optional.db.connection import connect_sqlite

logger = logging.getLogger(__name__)


# ============================================================
def _ensure_table(cur, create_sql: str):
    """
    CREATE TABLE IF NOT EXISTS を安全に実行
    """
    cur.execute(create_sql)


def _ensure_column(cur, table: str, column: str, coldef: str):
    """
    カラム存在確認 → 無ければ ADD
    """
    cols = {
        r[1] for r in cur.execute(f"PRAGMA table_info({table})")
    }
    if column not in cols:
        logger.info("➕ add column %s.%s", table, column)
        cur.execute(
            f"ALTER TABLE {table} ADD COLUMN {column} {coldef}"
        )


# ============================================================
def migrate_optional_db():
    """
    OPTIONAL DB を安全にマイグレーションする
    （ADD ONLY / DROP なし）
    """

    db_path: Path = get_path("optional_db")

    logger.info("⏳ OPTIONAL DB migration start: %s", db_path)

    con = connect_sqlite(db_path)
    cur = con.cursor()

    try:
        # ----------------------------------------------------
        # news_events
        # ----------------------------------------------------
        _ensure_table(
            cur,
            """
            CREATE TABLE IF NOT EXISTS news_events (
                symbol TEXT,
                symbolname TEXT,
                category TEXT,
                date TEXT
            )
            """
        )

        _ensure_column(cur, "news_events", "symbol", "TEXT")
        _ensure_column(cur, "news_events", "symbolname", "TEXT")
        _ensure_column(cur, "news_events", "category", "TEXT")
        _ensure_column(cur, "news_events", "date", "TEXT")

        # ----------------------------------------------------
        # margin_master
        # ----------------------------------------------------
        _ensure_table(
            cur,
            """
            CREATE TABLE IF NOT EXISTS margin_master (
                symbol TEXT PRIMARY KEY
            )
            """
        )

        # ----------------------------------------------------
        # daily_watchlist
        # ----------------------------------------------------
        _ensure_table(
            cur,
            """
            CREATE TABLE IF NOT EXISTS daily_watchlist (
                symbol TEXT PRIMARY KEY,
                reason TEXT,
                date TEXT
            )
            """
        )

        _ensure_column(cur, "daily_watchlist", "reason", "TEXT")
        _ensure_column(cur, "daily_watchlist", "date", "TEXT")

        logger.info("✅ OPTIONAL DB migration completed")

    except Exception:
        logger.exception("❌ OPTIONAL DB migration failed")
        raise

    finally:
        try:
            cur.close()
            con.close()
        except Exception:
            pass


# ============================================================
# 単体実行用
# ============================================================
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    migrate_optional_db()
