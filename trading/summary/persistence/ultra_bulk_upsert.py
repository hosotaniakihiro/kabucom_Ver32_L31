# ============================================================
# File   : trading/summary/persistence/ultra_bulk_upsert.py
# Version: Ver5.0-ULTRA-FAST-SUMMARY-BULK-UPSERT
# ------------------------------------------------------------
# ✔ SQLite ultra fast bulk upsert
# ✔ DataFrame → executemany
# ✔ WAL optimization
# ✔ retry on locked
# ✔ NaN / inf guard
# ✔ column auto detection
# ✔ dynamic UPSERT SQL
# ✔ production stable
# ============================================================

from __future__ import annotations

import sqlite3
import logging
import pandas as pd
import numpy as np
import time

logger = logging.getLogger(__name__)


# ============================================================
# SQLite connection
# ============================================================

def _connect(db_path):

    conn = sqlite3.connect(
        db_path,
        timeout=30,
        check_same_thread=False
    )

    cur = conn.cursor()

    # WAL mode
    cur.execute("PRAGMA journal_mode=WAL")

    # speed tuning
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.execute("PRAGMA temp_store=MEMORY")
    cur.execute("PRAGMA cache_size=-200000")

    # lock wait
    cur.execute("PRAGMA busy_timeout=5000")

    return conn


# ============================================================
# sanitize dataframe
# ============================================================

def _sanitize_df(df):

    if df is None or df.empty:
        return pd.DataFrame()

    df = df.copy()

    # replace NaN
    df = df.replace([np.inf, -np.inf], np.nan)
    df = df.where(pd.notnull(df), None)

    # symbol stabilization
    if "symbol" in df.columns:
        df["symbol"] = df["symbol"].astype(str)

    return df


# ============================================================
# build upsert SQL
# ============================================================

def _build_upsert_sql(table, columns):

    cols = ",".join(columns)

    placeholders = ",".join(["?"] * len(columns))

    update_cols = [
        f"{c}=excluded.{c}"
        for c in columns
        if c not in ("symbol", "datetime")
    ]

    update_clause = ",".join(update_cols)

    sql = f"""
    INSERT INTO {table} ({cols})
    VALUES ({placeholders})
    ON CONFLICT(symbol,datetime)
    DO UPDATE SET
    {update_clause}
    """

    return sql


# ============================================================
# dataframe → tuples
# ============================================================

def _df_to_tuples(df, columns):

    return [
        tuple(row[col] for col in columns)
        for _, row in df.iterrows()
    ]


# ============================================================
# bulk upsert
# ============================================================

def ultra_bulk_upsert(
    df: pd.DataFrame,
    db_path: str,
    table: str,
    retries: int = 5
):

    if df is None or df.empty:
        return

    df = _sanitize_df(df)

    columns = list(df.columns)

    sql = _build_upsert_sql(table, columns)

    data = _df_to_tuples(df, columns)

    conn = _connect(db_path)

    cur = conn.cursor()

    for i in range(retries):

        try:

            cur.executemany(sql, data)

            conn.commit()

            logger.info(
                "[ULTRA UPSERT] table=%s rows=%s",
                table,
                len(data)
            )

            break

        except sqlite3.OperationalError as e:

            if "locked" in str(e):

                logger.warning(
                    "[ULTRA UPSERT] database locked retry %s",
                    i + 1
                )

                time.sleep(0.2)

            else:
                raise

    conn.close()


# ============================================================
# interval wrapper
# ============================================================

def ultra_bulk_upsert_summary(
    df: pd.DataFrame,
    db_path: str,
    interval: int
):

    table_map = {
        1: "stock_summary_1min",
        3: "stock_summary_3min",
        5: "stock_summary_5min",
    }

    table = table_map.get(interval)

    if not table:
        raise ValueError(f"invalid interval: {interval}")

    ultra_bulk_upsert(
        df=df,
        db_path=db_path,
        table=table
    )