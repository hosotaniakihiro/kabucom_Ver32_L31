import sqlite3
import pandas as pd
from pathlib import Path
from database.session import summary_engine

base_dir = Path(r"X:\raw_data\kabu_station\summary")

sqlite_files = sorted(base_dir.glob("summary*.db"))

print("Found SQLite files:", len(sqlite_files))

with summary_engine.connect() as conn:

    for db_file in sqlite_files:

        print("Loading", db_file.name)

        with sqlite3.connect(db_file) as sconn:

            for tf in [1, 3, 5]:

                table = f"stock_summary_{tf}min"

                try:
                    df = pd.read_sql(f"SELECT * FROM {table}", sconn)
                except:
                    continue

                if df.empty:
                    continue

                df["symbol"] = df["symbol"].astype(str)

                # 🔥 id列を除外（超重要）
                if "id" in df.columns:
                    df = df.drop(columns=["id"])

                df.to_sql(
                    table,
                    conn,
                    if_exists="append",
                    index=False
                )

print("Migration completed.")