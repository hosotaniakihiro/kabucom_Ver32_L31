# ============================================================
# File   : tools/init_symbol_master.py
# ------------------------------------------------------------
# symbol_master DB 初期化スクリプト
# ・DBが無ければ作成
# ・symbol_master テーブルが無ければ作成
# ・既存DB/テーブルは絶対に壊さない（ADD ONLY）
# ============================================================

import sqlite3
import logging
from pathlib import Path

from config.paths import get_path

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# ============================================================
def init_symbol_master():
    """
    symbol_master DB / table を安全に初期化
    """

    db_path: Path = get_path("symbol_master_db")
    logger.info("symbol_master DB PATH = %s", db_path)

    # 親ディレクトリ作成
    db_path.parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(db_path) as conn:
        cur = conn.cursor()

        # ----------------------------------------------------
        # テーブル存在確認
        # ----------------------------------------------------
        cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='symbol_master'"
        )
        exists = cur.fetchone() is not None

        if exists:
            logger.info("symbol_master table already exists → skip create")
            return

        # ----------------------------------------------------
        # テーブル作成（最小構成）
        # ----------------------------------------------------
        logger.info("creating symbol_master table")

        cur.execute(
            """
            CREATE TABLE symbol_master (
                symbol      TEXT PRIMARY KEY,
                symbolname  TEXT,
                market      TEXT,
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at  TIMESTAMP
            )
            """
        )

        conn.commit()
        logger.info("symbol_master table created successfully")


# ============================================================
if __name__ == "__main__":
    init_symbol_master()
