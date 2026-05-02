# ============================================================
# ranking_schema_guard.py
# Ver1.0-FINAL-SCHEMA-SELF-HEAL
# ------------------------------------------------------------
# ✔ ranking_ma_1min の schema 自動修復
# ✔ datetime / created_at 不足を即補完
# ✔ 既存データ完全保持
# ✔ SQLite 専用・安全
# ============================================================

import logging
from sqlalchemy import text

logger = logging.getLogger(__name__)


def ensure_ranking_ma_schema(engine):
    """
    ranking_ma_1min の schema を self-heal する
    - datetime が無ければ ADD
    - created_at が無ければ ADD
    """

    with engine.begin() as conn:
        # --- カラム一覧取得 ---
        cols = conn.execute(
            text("PRAGMA table_info(ranking_ma_1min)")
        ).fetchall()

        if not cols:
            logger.warning(
                "[RANKING][SCHEMA] ranking_ma_1min not found → skip"
            )
            return

        col_names = {c[1] for c in cols}

        # --- datetime ---
        if "datetime" not in col_names:
            logger.warning(
                "[RANKING][SCHEMA] datetime column missing → ALTER TABLE"
            )
            conn.execute(
                text(
                    "ALTER TABLE ranking_ma_1min "
                    "ADD COLUMN datetime TEXT"
                )
            )

        # --- created_at ---
        if "created_at" not in col_names:
            logger.warning(
                "[RANKING][SCHEMA] created_at column missing → ALTER TABLE"
            )
            conn.execute(
                text(
                    "ALTER TABLE ranking_ma_1min "
                    "ADD COLUMN created_at TEXT"
                )
            )

        logger.info(
            "[RANKING][SCHEMA] ranking_ma_1min schema OK"
        )
