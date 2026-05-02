from __future__ import annotations

import sqlite3
from pathlib import Path

DB_PATHS = [
    r"\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\ranking\ranking20260428.db",
    r"\\192.168.0.22\AutoStockBuyAndSell\summary.db",
]

TARGET_TABLES = [
    "ranking_raw_1min",
    "ranking_snapshot_1min",
    "ranking_summary_1min",
    "ranking_summary_3min",
    "ranking_summary_5min",
    "stock_summary_1min",
    "stock_summary_3min",
    "stock_summary_5min",
]


def show_schema(db_path: str) -> None:
    path = Path(db_path)
    print("\n" + "=" * 80)
    print(f"DB: {path}")

    if not path.exists():
        print("❌ DB not found")
        return

    con = sqlite3.connect(str(path))
    try:
        tables = {
            r[0]
            for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }

        indexes = con.execute(
            "SELECT name, tbl_name, sql FROM sqlite_master WHERE type='index'"
        ).fetchall()

        for table in TARGET_TABLES:
            if table not in tables:
                print(f"\n❌ table missing: {table}")
                continue

            print(f"\n✅ table: {table}")

            cols = con.execute(f"PRAGMA table_info({table})").fetchall()
            for c in cols:
                print(f"  - {c[1]} {c[2]}")

            print("  indexes:")
            for name, tbl, sql in indexes:
                if tbl == table:
                    print(f"    - {name}: {sql}")

            try:
                cnt = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                print(f"  rows={cnt}")
            except Exception as e:
                print(f"  count failed: {e}")

    finally:
        con.close()


if __name__ == "__main__":
    for p in DB_PATHS:
        show_schema(p)