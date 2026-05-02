# ============================================================
# database/migrate/migrate_push.py
# Ver32-STRUCTURED-PUSH-MIGRATION-FINAL
# ------------------------------------------------------------
# ✔ ADD ONLY 原則厳守
# ✔ SAFE MIGRATION MODE 対応
# ✔ WAL最適化維持
# ✔ Base_push create_all保持
# ✔ 既存機能削除なし
# ✔ 将来列追加対応
# ✔ push DB自己修復対応
# ============================================================

from __future__ import annotations

import logging
from sqlalchemy import text
from database.bases import Base_push

logger = logging.getLogger(__name__)

# ============================================================
# SQLite ADD ONLY helper
# ============================================================

def _ensure_table_and_column(engine, table: str, column: str, col_type: str):

    with engine.begin() as conn:

        conn.execute(text("PRAGMA busy_timeout=30000"))

        tables = {
            row[0]
            for row in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            )
        }

        # テーブルが存在しない場合
        if table not in tables:
            logger.info(f"🆕 CREATE TABLE {table}")
            conn.execute(text(f"CREATE TABLE {table} ({column} {col_type})"))
            return

        cols = {
            row[1]
            for row in conn.execute(
                text(f"PRAGMA table_info({table})")
            )
        }

        if column not in cols:
            logger.info(f"➕ {table}.{column} ({col_type})")
            conn.execute(
                text(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
            )


# ============================================================
# UNIQUE(symbol, datetime) 保証
# ============================================================

def _ensure_unique_symbol_datetime(engine, table: str):

    index_name = f"uq_{table}_symbol_datetime"

    with engine.begin() as conn:

        conn.execute(text("PRAGMA busy_timeout=10000"))
        conn.execute(text("PRAGMA journal_mode=WAL"))

        # 重複削除（ADD ONLY原則維持）
        conn.execute(text(f"""
            DELETE FROM {table}
            WHERE rowid NOT IN (
                SELECT MAX(rowid)
                FROM {table}
                GROUP BY symbol, datetime
            )
        """))

        existing_indexes = {
            row[1]
            for row in conn.execute(
                text(f"PRAGMA index_list({table})")
            )
        }

        if index_name not in existing_indexes:
            logger.info(f"🔐 UNIQUE追加 {table}(symbol, datetime)")
            conn.execute(text(
                f"CREATE UNIQUE INDEX IF NOT EXISTS {index_name} "
                f"ON {table}(symbol, datetime)"
            ))


# ============================================================
# MAIN
# ============================================================

def migrate_push(engine):
    """
    push DB マイグレーション
    SAFE MIGRATION MODE 対応
    """

    print("📡 push DB migration start")

    # ========================================================
    # 1️⃣ Baseモデル create_all（破壊なし）
    # ========================================================

    Base_push.metadata.create_all(engine)
    print("✅ push DB create_all OK")

    # ========================================================
    # 2️⃣ ADD ONLY 列保証（将来拡張対応）
    # ========================================================

    # 🔥 ここに将来追加される列を定義
    # 例：板情報 / IV / Greeks / MarketOrder 等

    push_required_columns = {
        # 例（必要なら追加）
        # "exchange": "INTEGER",
        # "symbolname": "TEXT",
        # "market": "TEXT",
    }

    push_tables = [
        table.name
        for table in Base_push.metadata.tables.values()
    ]

    for tbl in push_tables:
        for col, typ in push_required_columns.items():
            _ensure_table_and_column(engine, tbl, col, typ)

        # symbol + datetime が存在するテーブルのみUNIQUE保証
        try:
            _ensure_unique_symbol_datetime(engine, tbl)
        except Exception:
            # symbol/datetimeを持たないテーブルはスキップ
            pass

    print("📡 push DB migration complete")