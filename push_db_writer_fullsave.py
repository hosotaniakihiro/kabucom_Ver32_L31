# ============================================================
# push_db_writer_fullsave.py（Ver24-FINAL）
# ------------------------------------------------------------
# push_df の "全行" を DB に保存する高速版（検証用）
# ============================================================

import sqlite3
import logging
from global_state import global_data

logger = logging.getLogger(__name__)


def save_all_rows(db_path):
    """
    push_df の全行を db_path の DB に保存（検証用）
    """
    df = global_data.get_push_df()
    if df is None or df.empty:
        print("No push_df to save.")
        return

    conn = sqlite3.connect(db_path)

    try:
        # Create table
        sql = """
        CREATE TABLE IF NOT EXISTS stream_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            time VARCHAR,
            datetime VARCHAR,
            content VARCHAR,
            symbol VARCHAR,
            symbolname VARCHAR,
            price FLOAT,
            volume FLOAT,
            trading_value FLOAT,
            vwap FLOAT,
            previousclose FLOAT,
            high_price FLOAT,
            low_price FLOAT,
            bid_price FLOAT,
            bid_qty FLOAT,
            ask_price FLOAT,
            ask_qty FLOAT
        );
        """
        conn.execute(sql)

        # Insert SQL
        insert_sql = """
        INSERT INTO stream_data (
            time, datetime, content,
            symbol, symbolname,
            price, volume, trading_value,
            vwap, previousclose,
            high_price, low_price,
            bid_price, bid_qty, ask_price, ask_qty
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """

        # Convert DataFrame → list of rows
        rows = []
        for _, row in df.iterrows():
            d = row.to_dict()
            rows.append([
                str(d.get("time")),
                str(d.get("datetime")),
                d.get("content", ""),
                d.get("symbol"),
                d.get("symbolname"),
                d.get("price"),
                d.get("volume"),
                d.get("trading_value"),
                d.get("vwap"),
                d.get("previousclose"),
                d.get("high_price"),
                d.get("low_price"),
                d.get("bid_price"),
                d.get("bid_qty"),
                d.get("ask_price"),
                d.get("ask_qty"),
            ])

        # Bulk insert
        conn.executemany(insert_sql, rows)
        conn.commit()

        print(f"Saved {len(rows)} rows to DB → {db_path}")

    except Exception as e:
        logger.error(f"[push_db_writer_fullsave] error: {e}", exc_info=True)

    finally:
        conn.close()
