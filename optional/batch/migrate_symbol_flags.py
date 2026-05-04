# ============================================================
# OPTIONAL/batch/migrate_symbol_flags.py
# ------------------------------------------------------------
# ・symbol_flags.db のマイグレーション
# ・ETF判定用 is_etf カラムを永続化
# ・何度実行しても安全（冪等）
# ・paths.py 前提（Y:/ 直書き禁止）
# ============================================================

import sqlite3
import logging
from pathlib import Path

from config.paths import get_path

logger = logging.getLogger(__name__)

# ------------------------------------------------------------
# paths.py 経由
# ------------------------------------------------------------
DB_FLAGS: Path = get_path("symbol_flags_db")


# ------------------------------------------------------------
# helper
# ------------------------------------------------------------
def ensure_table(cur: sqlite3.Cursor):
    """
    symbol_flags テーブル保証（完全定義）
    """
    cur.execute("""
        CREATE TABLE IF NOT EXISTS symbol_flags (
            symbol TEXT PRIMARY KEY,
            symbolname TEXT,
            is_margin INTEGER DEFAULT 0,
            is_attention INTEGER DEFAULT 0,
            is_etf INTEGER DEFAULT 0,
            updated_at TEXT
        )
    """)


def ensure_column(cur: sqlite3.Cursor, table: str, column: str, col_type: str):
    """
    column が無ければ ALTER TABLE ADD COLUMN
    """
    cur.execute(f"PRAGMA table_info({table})")
    cols = [r[1] for r in cur.fetchall()]

    if column not in cols:
        logger.info(f"➕ add column: {table}.{column} ({col_type})")
        cur.execute(
            f"ALTER TABLE {table} ADD COLUMN {column} {col_type}"
        )


# ------------------------------------------------------------
# main
# ------------------------------------------------------------
def migrate_symbol_flags(db_path: Path | None = None):
    """
    symbol_flags.db マイグレーション
    """
    db_path = Path(db_path) if db_path else DB_FLAGS

    if not db_path.exists():
        logger.error(f"❌ symbol_flags DB not found: {db_path}")
        return

    logger.info("=" * 60)
    logger.info("⏳ migrate_symbol_flags START")
    logger.info(f" DB = {db_path}")
    logger.info("=" * 60)

    with sqlite3.connect(db_path) as con:
        cur = con.cursor()

        # ----------------------------------------------------
        # table ensure
        # ----------------------------------------------------
        ensure_table(cur)

        # ----------------------------------------------------
        # column ensure（後方互換）
        # ----------------------------------------------------
        ensure_column(cur, "symbol_flags", "symbolname", "TEXT")
        ensure_column(cur, "symbol_flags", "is_margin", "INTEGER DEFAULT 0")
        ensure_column(cur, "symbol_flags", "is_attention", "INTEGER DEFAULT 0")
        ensure_column(cur, "symbol_flags", "is_etf", "INTEGER DEFAULT 0")
        ensure_column(cur, "symbol_flags", "updated_at", "TEXT")

        con.commit()

    logger.info("=" * 60)
    logger.info("✅ migrate_symbol_flags DONE")
    logger.info("=" * 60)


# ------------------------------------------------------------
# entry point
# ------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    migrate_symbol_flags()
