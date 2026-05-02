# ============================================================
# File   : trading/ranking/tonosama/snapshot_loader.py
# Version: PRODUCTION-STABLE-REV1.1-LATEST-DB-TIME-FALLBACK
# Purpose:
#   ranking_snapshot_1min 読み込み
#
# 修正点:
#   - datetime / snapshot_time 両対応
#   - 指定時間範囲で空の場合、DB内の最新時刻を基準に再読込
#   - 夜間・休場日テストでも ranking_snapshot_1min を読める
# ============================================================

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)


def _table_columns(con: sqlite3.Connection, table: str) -> list[str]:
    try:
        rows = con.execute(f"PRAGMA table_info({table})").fetchall()
        return [str(r[1]) for r in rows]
    except Exception:
        return []


def _pick_time_col(columns: list[str]) -> str:
    if "datetime" in columns:
        return "datetime"
    if "snapshot_time" in columns:
        return "snapshot_time"
    return "datetime"


def _read_sql(
    con: sqlite3.Connection,
    table: str,
    time_col: str,
    *,
    start_dt: Optional[str],
    end_dt: Optional[str],
) -> pd.DataFrame:
    where = []
    params = []

    if start_dt:
        where.append(f"{time_col} >= ?")
        params.append(start_dt)

    if end_dt:
        where.append(f"{time_col} <= ?")
        params.append(end_dt)

    where_sql = ""
    if where:
        where_sql = " WHERE " + " AND ".join(where)

    sql = f"""
        SELECT *
        FROM {table}
        {where_sql}
        ORDER BY {time_col} ASC
    """

    return pd.read_sql_query(sql, con, params=params)


def _read_latest_window(
    con: sqlite3.Connection,
    table: str,
    time_col: str,
    *,
    minutes: int = 60,
) -> pd.DataFrame:
    latest_sql = f"SELECT MAX({time_col}) AS latest_dt FROM {table}"
    latest_df = pd.read_sql_query(latest_sql, con)

    if latest_df.empty:
        return pd.DataFrame()

    latest = latest_df.iloc[0].get("latest_dt")
    latest_ts = pd.to_datetime(latest, errors="coerce")

    if pd.isna(latest_ts):
        return pd.DataFrame()

    start_ts = latest_ts - pd.Timedelta(minutes=int(minutes))

    sql = f"""
        SELECT *
        FROM {table}
        WHERE {time_col} >= ?
          AND {time_col} <= ?
        ORDER BY {time_col} ASC
    """

    return pd.read_sql_query(
        sql,
        con,
        params=[
            start_ts.strftime("%Y-%m-%d %H:%M:%S"),
            latest_ts.strftime("%Y-%m-%d %H:%M:%S"),
        ],
    )


def load_ranking_snapshot_1min(
    db_path: str | Path,
    *,
    start_dt: Optional[str] = None,
    end_dt: Optional[str] = None,
    table: str = "ranking_snapshot_1min",
    fallback_latest_window_minutes: int = 60,
) -> pd.DataFrame:
    path = str(db_path)

    try:
        with sqlite3.connect(path, timeout=10) as con:
            columns = _table_columns(con, table)

            if not columns:
                logger.warning(
                    "[RANKING TONOSAMA LOADER] table not found or no columns path=%s table=%s",
                    path,
                    table,
                )
                return pd.DataFrame()

            time_col = _pick_time_col(columns)

            df = _read_sql(
                con,
                table,
                time_col,
                start_dt=start_dt,
                end_dt=end_dt,
            )

            if df.empty and (start_dt or end_dt):
                logger.warning(
                    "[RANKING TONOSAMA LOADER] empty by requested range -> fallback latest window "
                    "path=%s table=%s time_col=%s start=%s end=%s minutes=%s",
                    path,
                    table,
                    time_col,
                    start_dt,
                    end_dt,
                    fallback_latest_window_minutes,
                )

                df = _read_latest_window(
                    con,
                    table,
                    time_col,
                    minutes=fallback_latest_window_minutes,
                )

    except Exception:
        logger.exception("[RANKING TONOSAMA LOADER] failed path=%s table=%s", path, table)
        return pd.DataFrame()

    if df.empty:
        logger.info("[RANKING TONOSAMA LOADER] empty path=%s table=%s", path, table)
        return df

    if "datetime" not in df.columns and "snapshot_time" in df.columns:
        df["datetime"] = df["snapshot_time"]

    if "datetime" in df.columns:
        df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")

    if "snapshot_time" in df.columns:
        df["snapshot_time"] = pd.to_datetime(df["snapshot_time"], errors="coerce")

    if "symbol" in df.columns:
        df["symbol"] = df["symbol"].astype(str)

    logger.info(
        "[RANKING TONOSAMA LOADER] loaded rows=%s path=%s table=%s start=%s end=%s",
        len(df),
        path,
        table,
        df["datetime"].min() if "datetime" in df.columns else "-",
        df["datetime"].max() if "datetime" in df.columns else "-",
    )

    return df