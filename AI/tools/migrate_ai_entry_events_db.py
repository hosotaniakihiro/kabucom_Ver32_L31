# ============================================================
# ai_entry_events.db FULL schema migration
# ------------------------------------------------------------
# ✔ entry_event_saver / exit / snapshot 全対応
# ✔ 何度実行しても安全
# ============================================================

import sqlite3
import logging
from config.paths import get_path

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DB_FILE = get_path("ai_entry_events_db")
TABLE = "entry_events"


def get_columns(cur):
    cur.execute(f"PRAGMA table_info({TABLE})")
    return {row[1] for row in cur.fetchall()}


def add_column(cur, col_def):
    col = col_def.split()[0]
    if col not in existing_cols:
        logger.info(f"[MIGRATE] add column: {col}")
        cur.execute(f"ALTER TABLE {TABLE} ADD COLUMN {col_def}")
    else:
        logger.info(f"[MIGRATE] column exists: {col}")


def main():
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()

    global existing_cols
    existing_cols = get_columns(cur)

    # ========================================================
    # ENTRY 基本
    # ========================================================
    add_column(cur, "datetime TEXT")
    add_column(cur, "symbol TEXT")
    add_column(cur, "side TEXT")
    add_column(cur, "entry_price REAL")
    add_column(cur, "interval INTEGER")
    add_column(cur, "score REAL")

    # ========================================================
    # 実行コンテキスト
    # ========================================================
    add_column(cur, "source TEXT")
    add_column(cur, "entry_mode TEXT")
    add_column(cur, "order_type TEXT")

    # ========================================================
    # AI 判定
    # ========================================================
    add_column(cur, "ai_confidence REAL")
    add_column(cur, "dominant_ratio REAL")
    add_column(cur, "model_used TEXT")

    # ========================================================
    # 即益 / HOLDTIME
    # ========================================================
    add_column(cur, "ai_pred REAL")
    add_column(cur, "ai_threshold REAL")
    add_column(cur, "ai_pass INTEGER")
    add_column(cur, "pred_hold_seconds INTEGER")

    # ========================================================
    # 市場環境
    # ========================================================
    add_column(cur, "index_shock INTEGER")

    # ========================================================
    # 特徴量
    # ========================================================
    add_column(cur, "features_json TEXT")

    # ========================================================
    # EXIT 系
    # ========================================================
    add_column(cur, "exit_time TEXT")
    add_column(cur, "exit_price REAL")
    add_column(cur, "exit_reason TEXT")
    add_column(cur, "exit_confidence REAL")

    # ========================================================
    # パフォーマンス
    # ========================================================
    add_column(cur, "pnl REAL")
    add_column(cur, "holding_seconds INTEGER")
    add_column(cur, "max_mfe REAL")
    add_column(cur, "max_mae REAL")

    # ========================================================
    # 思考トレース
    # ========================================================
    add_column(cur, "exit_check_snapshots TEXT")

    # ========================================================
    # メタ
    # ========================================================
    add_column(cur, "created_at TEXT DEFAULT (datetime('now'))")

    con.commit()
    con.close()

    logger.info("[MIGRATE] schema sync completed")


if __name__ == "__main__":
    main()
