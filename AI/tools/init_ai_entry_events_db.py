# ============================================================
# File   : tools/init_ai_entry_events_db.py
# ------------------------------------------------------------
# ✔ ai_entry_events.db 初期化スクリプト
# ✔ DB / テーブルが無ければ作成
# ✔ 既存DBがあっても安全（IF NOT EXISTS）
# ✔ 何度実行しても副作用なし
# ============================================================

import sqlite3
import logging
from pathlib import Path

from config.paths import get_path, ensure_dirs

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# ============================================================
# MAIN
# ============================================================

def main():
    # --------------------------------------------------------
    # ディレクトリ作成（raw_data/AI など）
    # --------------------------------------------------------
    ensure_dirs()

    db_path: Path = get_path("ai_entry_events_db")

    logger.info(f"[INIT] target db = {db_path}")

    # --------------------------------------------------------
    # DB 接続（存在しなければ作成される）
    # --------------------------------------------------------
    con = sqlite3.connect(db_path)
    cur = con.cursor()

    # --------------------------------------------------------
    # entry_events テーブル作成
    # --------------------------------------------------------
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS entry_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,

            -- basic
            symbol TEXT NOT NULL,
            entry_time TEXT NOT NULL,
            entry_price REAL NOT NULL,
            side TEXT,

            -- entry AI
            score_total REAL,
            dominant_ratio REAL,
            entry_confidence REAL,
            entry_reason TEXT,

            -- exit
            exit_time TEXT,
            exit_price REAL,
            exit_reason TEXT,
            exit_confidence REAL,

            -- performance
            pnl REAL,
            holding_seconds INTEGER,
            max_mfe REAL,
            max_mae REAL,

            -- snapshot / explainability
            exit_check_snapshots TEXT,

            -- meta
            source TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )
        """
    )

    con.commit()
    con.close()

    logger.info("[INIT] entry_events table ensured")
    logger.info("[INIT] done")


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    main()
