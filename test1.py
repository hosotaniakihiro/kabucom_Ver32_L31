# ============================================================
# SUMMARY TABLE REBUILD MIGRATION（FINAL FIXED）
# ------------------------------------------------------------
# ✔ 全カラム自動取得
# ✔ UNIQUE(symbol, datetime)
# ✔ 列ズレ完全防止
# ✔ NULL / 空補完
# ✔ production safe
# ============================================================

import sqlite3
import os

DB_PATH = r"\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\summary\summary20260319.db"

TABLES = [
    "stock_summary_1min",
    "stock_summary_3min",
    "stock_summary_5min",
]


def get_columns(conn, table):
    cursor = conn.execute(f"PRAGMA table_info({table})")
    return [row[1] for row in cursor.fetchall()]


def rebuild_table(conn, table):

    print(f"\n🔧 REBUILD: {table}")

    tmp = f"{table}_new"

    cols = get_columns(conn, table)

    col_str = ", ".join(cols)

    # ----------------------------------------------------
    # 新テーブル（構造コピー）
    # ----------------------------------------------------
    conn.execute(f"""
        CREATE TABLE {tmp} AS
        SELECT * FROM {table} WHERE 0
    """)

    # ----------------------------------------------------
    # UNIQUE（新）
    # ----------------------------------------------------
    conn.execute(f"""
        CREATE UNIQUE INDEX idx_{tmp}_symbol_datetime
        ON {tmp}(symbol, datetime)
    """)

    # ----------------------------------------------------
    # SELECT句生成（補完込み）
    # ----------------------------------------------------
    select_expr = []

    for c in cols:

        if c == "date":
            expr = "CASE WHEN date='' OR date IS NULL THEN substr(datetime,1,10) ELSE date END AS date"

        elif c == "time_range":
            expr = "CASE WHEN time_range='' OR time_range IS NULL THEN time ELSE time_range END AS time_range"

        else:
            expr = c

        select_expr.append(expr)

    select_sql = ",\n            ".join(select_expr)

    # ----------------------------------------------------
    # INSERT
    # ----------------------------------------------------
    conn.execute(f"""
        INSERT INTO {tmp} ({col_str})
        SELECT
            {select_sql}
        FROM {table}
    """)

    # ----------------------------------------------------
    # swap
    # ----------------------------------------------------
    conn.execute(f"DROP TABLE {table}")
    conn.execute(f"ALTER TABLE {tmp} RENAME TO {table}")

    print(f"✅ REBUILT: {table}")


def main():

    if not os.path.exists(DB_PATH):
        raise FileNotFoundError(DB_PATH)

    conn = sqlite3.connect(DB_PATH)

    try:

        print("▶ START REBUILD MIGRATION")

        conn.execute("PRAGMA journal_mode=WAL;")

        for table in TABLES:
            rebuild_table(conn, table)

        conn.commit()

        print("\n🚀 ALL COMPLETE")

    except Exception as e:

        conn.rollback()
        print("❌ FAILED:", e)
        raise

    finally:
        conn.close()


if __name__ == "__main__":
    main()