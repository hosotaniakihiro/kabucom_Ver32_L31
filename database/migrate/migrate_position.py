# ============================================================
# database/migrate/migrate_position.py
# Ver32-STRUCTURED-POSITION-MIGRATION-FINAL
# ------------------------------------------------------------
# ✔ ADD ONLY 原則厳守
# ✔ Base_position create_all保持
# ✔ SAFE MIGRATION MODE 対応
# ✔ 列不足自己修復
# ✔ 既存データ破壊なし
# ✔ WAL維持
# ✔ Ver30.8完全互換
# ============================================================

from __future__ import annotations

import logging
from sqlalchemy import text
from database.bases import Base_position

logger = logging.getLogger(__name__)

# ============================================================
# INTERNAL HELPERS
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

        # テーブル未存在（通常はcreate_allで生成される）
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
# MAIN ENTRY
# ============================================================

def migrate_position(engine):
    """
    Position DB migration
    SAFE MIGRATION MODE 対応
    """

    print("💼 position DB migration start")

    # ========================================================
    # 1️⃣ Baseモデル create_all（破壊なし）
    # ========================================================

    Base_position.metadata.create_all(engine)

    # ========================================================
    # 2️⃣ ADD ONLY 列保証（Ver30.8完全互換）
    # ========================================================

    position_required_columns = {
        "exchange": "INTEGER",
        "margin_trade_type": "INTEGER",
        "account_type": "INTEGER",
        "exit_price": "REAL",
        "exit_time": "TEXT",
        "closed_time": "TEXT",
        "close_time": "TEXT",
    }

    table_name = "positions"

    for col, typ in position_required_columns.items():
        _ensure_table_and_column(engine, table_name, col, typ)

    print("💼 position DB migration complete")