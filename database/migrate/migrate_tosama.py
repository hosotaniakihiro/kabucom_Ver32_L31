# ============================================================
# database/migrate/migrate_tosama.py
# Ver32-STRUCTURED-TOSAMA-MIGRATION-FINAL
# ------------------------------------------------------------
# ✔ ADD ONLY 原則厳守
# ✔ ranking_snapshot_1min mirror 保証
# ✔ ranking_ma_1min mirror 保証
# ✔ SAFE MIGRATION MODE 対応
# ✔ 既存データ破壊なし
# ✔ Ver30.8完全互換
# ✔ WAL維持
# ============================================================

from __future__ import annotations

import logging
from sqlalchemy import text

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

def migrate_tosama(engine):
    """
    Tosama mirror DB migration
    SAFE MIGRATION MODE 対応
    """

    print("🧠 tosama DB migration start")

    # ========================================================
    # 1️⃣ ranking_snapshot_1min mirror 列保証
    # ========================================================

    ranking_snapshot_columns = {
        "symbol": "TEXT",
        "symbolname": "TEXT",
        "rank_type": "TEXT",
        "rank_type_id": "INTEGER",
        "market": "TEXT",
        "rank_position": "INTEGER",
        "current_price": "REAL",
        "trading_volume": "REAL",
        "volume_speed": "REAL",
        "rank_strength": "REAL",
        "rank_persistence": "INTEGER",
        "rank_delta": "INTEGER",
        "price_delta_1m": "REAL",
        "volume_delta_1m": "REAL",
        "minute_of_day": "INTEGER",
        "snapshot_time": "TEXT",
        "source": "TEXT",
    }

    for col, typ in ranking_snapshot_columns.items():
        _ensure_table_and_column(engine, "ranking_snapshot_1min", col, typ)

    print("🧠 tosama ranking_snapshot_1min OK")

    # ========================================================
    # 2️⃣ ranking_ma_1min mirror 列保証
    # ========================================================

    ranking_ma_columns = {
        "symbol": "TEXT",
        "rank_type": "TEXT",
        "market": "TEXT",
        "ma_rank_position": "REAL",
        "ma_volume_speed": "REAL",
        "trend_score": "REAL",
        "snapshot_time": "TEXT",
        "created_at": "TEXT",
    }

    for col, typ in ranking_ma_columns.items():
        _ensure_table_and_column(engine, "ranking_ma_1min", col, typ)

    print("🧠 tosama ranking_ma_1min OK")

    print("🧠 tosama DB migration complete")