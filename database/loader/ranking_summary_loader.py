# ============================================================
# File   : database/loader/ranking_summary_loader.py
# Version: PRODUCTION-STABLE-REV1.0-RANKING-SUMMARY-LOADER
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import sqlite3
from typing import Optional

import pandas as pd

from database.paths.ranking_paths import DEFAULT_RANKING_DIR, get_ranking_db_path
from database.schema.ranking_summary_schema import (
    RANKING_SUMMARY_DB_LOCK,
    ensure_ranking_summary_table,
    get_existing_columns,
    table_exists,
    table_name,
)
from database.sqlite import is_lock_error, quote_ident

logger = logging.getLogger(__name__)


def load_latest_ranking_summary(
    *,
    interval: int,
    trade_date=None,
    db_path: Optional[str] = None,
    ranking_dir: str = DEFAULT_RANKING_DIR,
    limit_minutes: int = 240,
) -> pd.DataFrame:
    """
    ranking_summary_* から直近データを読む。
    """
    interval = int(interval)
    table = table_name(interval)
    path = db_path or get_ranking_db_path(trade_date, ranking_dir=ranking_dir)

    if not os.path.exists(path):
        logger.warning("[RANKING SUMMARY LOAD] db not found path=%s", path)
        return pd.DataFrame()

    since_dt = dt.datetime.now() - dt.timedelta(minutes=int(limit_minutes))

    try:
        with RANKING_SUMMARY_DB_LOCK:
            with sqlite3.connect(path, timeout=10) as con:
                con.execute("PRAGMA busy_timeout=10000")

                try:
                    ensure_ranking_summary_table(con, interval=interval)
                except sqlite3.OperationalError as e:
                    if is_sqlite_error(e):
                        logger.warning(
                            "[RANKING SUMMARY LOAD] ensure table skipped by locked table=%s err=%s",
                            table,
                            e,
                        )
                    else:
                        raise
                except Exception:
                    logger.warning(
                        "[RANKING SUMMARY LOAD] ensure table skipped table=%s",
                        table,
                        exc_info=True,
                    )

                if not table_exists(con, table):
                    logger.warning("[RANKING SUMMARY LOAD] table not found %s", table)
                    return pd.DataFrame()

                df = pd.read_sql_query(
                    f"""
                    SELECT *
                      FROM {quote_ident(table)}
                     WHERE datetime >= ?
                     ORDER BY datetime ASC
                    """,
                    con,
                    params=[since_dt.strftime("%Y-%m-%d %H:%M:%S")],
                )

    except Exception:
        logger.exception("[RANKING SUMMARY LOAD] failed table=%s path=%s", table, path)
        return pd.DataFrame()

    if df.empty:
        return df

    df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
    return df


def load_ranking_summary_at_latest_slot(
    *,
    interval: int,
    trade_date=None,
    db_path: Optional[str] = None,
    ranking_dir: str = DEFAULT_RANKING_DIR,
) -> pd.DataFrame:
    """
    ranking_summary_* の最新 datetime の行だけを読む。
    """
    interval = int(interval)
    table = table_name(interval)
    path = db_path or get_ranking_db_path(trade_date, ranking_dir=ranking_dir)

    if not os.path.exists(path):
        logger.warning("[RANKING SUMMARY LOAD SLOT] db not found path=%s", path)
        return pd.DataFrame()

    try:
        with RANKING_SUMMARY_DB_LOCK:
            with sqlite3.connect(path, timeout=10) as con:
                con.execute("PRAGMA busy_timeout=10000")

                try:
                    ensure_ranking_summary_table(con, interval=interval)
                except sqlite3.OperationalError as e:
                    if is_locked_error(e):
                        logger.warning(
                            "[RANKING SUMMARY LOAD SLOT] ensure table skipped by locked table=%s err=%s",
                            table,
                            e,
                        )
                    else:
                        raise
                except Exception:
                    logger.warning(
                        "[RANKING SUMMARY LOAD SLOT] ensure table skipped table=%s",
                        table,
                        exc_info=True,
                    )

                if not table_exists(con, table):
                    logger.warning("[RANKING SUMMARY LOAD SLOT] table not found %s", table)
                    return pd.DataFrame()

                row = con.execute(
                    f"""
                    SELECT MAX(datetime)
                      FROM {quote_ident(table)}
                     WHERE datetime IS NOT NULL
                       AND TRIM(CAST(datetime AS TEXT)) <> ''
                    """
                ).fetchone()

                latest_dt = row[0] if row else None
                if not latest_dt:
                    return pd.DataFrame()

                df = pd.read_sql_query(
                    f"""
                    SELECT *
                      FROM {quote_ident(table)}
                     WHERE datetime = ?
                     ORDER BY display_score DESC, final_score DESC, score DESC
                    """,
                    con,
                    params=[latest_dt],
                )

    except Exception:
        logger.exception("[RANKING SUMMARY LOAD SLOT] failed table=%s path=%s", table, path)
        return pd.DataFrame()

    if df.empty:
        return df

    df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
    return df


def get_ranking_summary_schema_columns(
    *,
    interval: int,
    trade_date=None,
    db_path: Optional[str] = None,
    ranking_dir: str = DEFAULT_RANKING_DIR,
) -> list[str]:
    """
    ranking_summary_* の既存カラム一覧を返す。
    """
    interval = int(interval)
    table = table_name(interval)
    path = db_path or get_ranking_db_path(trade_date, ranking_dir=ranking_dir)

    if not os.path.exists(path):
        return []

    try:
        with RANKING_SUMMARY_DB_LOCK:
            with sqlite3.connect(path, timeout=10) as con:
                con.execute("PRAGMA busy_timeout=10000")
                if not table_exists(con, table):
                    return []

                cols = get_existing_columns(con, table)
                return list(cols.keys())

    except Exception:
        logger.exception(
            "[RANKING SUMMARY LOAD] schema columns failed table=%s path=%s",
            table,
            path,
        )
        return []


__all__ = [
    "load_latest_ranking_summary",
    "load_ranking_summary_at_latest_slot",
    "get_ranking_summary_schema_columns",
]