# =========================================
# batch/migrate_optional_db.py
# =========================================

import sqlite3

DB_OPTIONAL = r"Y:/stock_optional_data/optional_data.db"

def migrate():
    with sqlite3.connect(DB_OPTIONAL) as con:
        cur = con.cursor()

        # ------------------------------
        # daily_watchlist（クリーン再作成）
        # ------------------------------
        cur.execute("DROP TABLE IF EXISTS daily_watchlist")
        cur.execute("""
        CREATE TABLE daily_watchlist (
            symbol TEXT,
            symbolname TEXT,
            date TEXT,

            buy_score REAL DEFAULT 0,
            sell_score REAL DEFAULT 0,

            reason_buy TEXT,
            reason_sell TEXT,

            PRIMARY KEY(symbol, date)
        )
        """)

        cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_daily_watchlist_date
        ON daily_watchlist(date)
        """)

        # ------------------------------
        # pts_rank（★忘れがち）
        # ------------------------------
        cur.execute("""
        CREATE TABLE IF NOT EXISTS pts_rank (
            symbol TEXT,
            symbolname TEXT,
            market TEXT,

            close_price REAL,
            pts_price REAL,
            pts_diff REAL,
            change_pct REAL,
            pts_volume REAL,

            per REAL,
            pbr REAL,
            yield REAL,

            rank_type TEXT,
            score REAL,

            date TEXT,
            fetched_at TEXT,

            PRIMARY KEY(symbol, rank_type, date)
        )
        """)

        cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_pts_rank_date
        ON pts_rank(date)
        """)

        con.commit()

    print("✅ optional_data.db initialized (daily_watchlist + pts_rank)")

if __name__ == "__main__":
    migrate()
