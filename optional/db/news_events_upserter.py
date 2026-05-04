# ============================================================
# optional/db/news_events_upserter.py
# ------------------------------------------------------------
# ・news_events UPSERT 専用
# ・marketnews / kessan / surprise 共通
# ・再実行完全安全
# ============================================================

import sqlite3
import logging
from pathlib import Path
from datetime import datetime
import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# テーブル作成
# ============================================================
def ensure_news_events_table(conn: sqlite3.Connection):
    sql = """
    CREATE TABLE IF NOT EXISTS news_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL,
        symbol TEXT NOT NULL,
        symbolname TEXT,
        headline TEXT NOT NULL,
        category TEXT NOT NULL,
        source TEXT,
        created_at TEXT NOT NULL,
        UNIQUE(date, symbol, category, headline)
    );
    """
    conn.execute(sql)
    conn.commit()


# ============================================================
# UPSERT 本体
# ============================================================
def upsert_news_events(
    df: pd.DataFrame,
    db_path: Path,
) -> dict:
    """
    news_events へ UPSERT

    Returns:
        {
          "inserted": int,
          "skipped": int
        }
    """

    if df is None or df.empty:
        logger.info("ℹ news_events df empty -> skip")
        return {"inserted": 0, "skipped": 0}

    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    inserted = 0
    skipped = 0

    now = datetime.now().isoformat(timespec="seconds")

    with sqlite3.connect(db_path) as conn:
        ensure_news_events_table(conn)

        cur = conn.cursor()

        for _, row in df.iterrows():
            try:
                cur.execute(
                    """
                    INSERT OR IGNORE INTO news_events (
                        date,
                        symbol,
                        symbolname,
                        headline,
                        category,
                        source,
                        created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(row.get("date")),
                        str(row.get("symbol")),
                        row.get("symbolname"),
                        row.get("headline"),
                        row.get("category"),
                        row.get("source"),
                        now,
                    ),
                )

                if cur.rowcount == 1:
                    inserted += 1
                else:
                    skipped += 1

            except Exception:
                logger.exception(
                    "❌ failed to upsert news_event row=%s",
                    dict(row),
                )

        conn.commit()

    logger.info(
        "💾 news_events UPSERT done inserted=%d skipped=%d",
        inserted,
        skipped,
    )

    return {
        "inserted": inserted,
        "skipped": skipped,
    }
