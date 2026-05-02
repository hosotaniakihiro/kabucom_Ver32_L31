# ============================================================
# database/schema_guard.py
# Ver1.0-POSITION-SCHEMA-SELF-HEAL
# ------------------------------------------------------------
# ✔ positions テーブルの schema 自動修復
# ✔ 既存データ完全保持
# ✔ SQLite 専用・安全
# ============================================================

from sqlalchemy import text
import logging

logger = logging.getLogger(__name__)


def ensure_positions_schema(engine):
    """
    positions テーブルに必要なカラムを self-heal する
    """
    required_columns = {
        "price": "REAL",
        "exchange": "INTEGER",
        "execution_id": "VARCHAR",
    }

    with engine.begin() as conn:
        # テーブル存在確認
        tables = conn.execute(
            text(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='positions'"
            )
        ).fetchone()

        if not tables:
            logger.warning(
                "[SCHEMA][POSITION] positions table not found → skip"
            )
            return

        cols = conn.execute(
            text("PRAGMA table_info(positions)")
        ).fetchall()

        existing = {c[1] for c in cols}

        for col, ddl in required_columns.items():
            if col not in existing:
                logger.warning(
                    f"[SCHEMA][POSITION] missing '{col}' → ALTER TABLE"
                )
                conn.execute(
                    text(
                        f"ALTER TABLE positions "
                        f"ADD COLUMN {col} {ddl}"
                    )
                )

        logger.info("[SCHEMA][POSITION] positions schema OK")
