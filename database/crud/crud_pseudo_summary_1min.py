# ============================================================
# database/crud/crud_pseudo_summary_1min.py
# Ver1.0-FINAL-PSEUDO-1M-DB
# ------------------------------------------------------------
# ✔ 疑似1m（RANKING_PSEUDO）専用
# ✔ 当日DBのみ
# ✔ 再起動耐性
# ============================================================

import sqlite3
import datetime as dt
import pandas as pd
from pathlib import Path

from config.paths import get_path

TABLE = "pseudo_summary_1min"


def _db_path() -> Path:
    today = dt.datetime.now().strftime("%Y%m%d")
    return get_path("summary_db_dir") / f"pseudo_summary_1min_{today}.db"


def ensure_table():
    db = _db_path()
    db.parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(db) as con:
        con.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {TABLE} (
                symbol TEXT,
                symbolname TEXT,
                open_price REAL,
                high_price REAL,
                low_price REAL,
                close_price REAL,
                volume REAL,
                end_time TEXT,
                pseudo_ema5 REAL,
                pseudo_ema13 REAL,
                pseudo_ema21 REAL,
                source TEXT,
                is_pseudo INTEGER
            )
            """
        )


def insert_pseudo_1m(df: pd.DataFrame):
    if df is None or df.empty:
        return

    ensure_table()
    db = _db_path()

    cols = [
        "symbol", "symbolname",
        "open_price", "high_price", "low_price", "close_price",
        "volume", "end_time",
        "pseudo_ema5", "pseudo_ema13", "pseudo_ema21",
        "source",
    ]

    d = df[df["source"] == "RANKING_PSEUDO"].copy()
    if d.empty:
        return

    d["is_pseudo"] = 1
    d = d[cols + ["is_pseudo"]]

    with sqlite3.connect(db) as con:
        d.to_sql(TABLE, con, if_exists="append", index=False)


def load_today_pseudo_1m() -> pd.DataFrame:
    db = _db_path()
    if not db.exists():
        return pd.DataFrame()

    with sqlite3.connect(db) as con:
        return pd.read_sql(f"SELECT * FROM {TABLE}", con)
