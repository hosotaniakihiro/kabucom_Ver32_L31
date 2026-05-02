# ============================================================
# File   : trading/ranking/summary/loader.py
# Ver    : PRODUCTION-STABLE-REV1.0-RANKING-SUMMARY-LOADER
# ------------------------------------------------------------
# 【概要】
#   ranking_snapshot_1min の読み込み専用モジュール
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import sqlite3
from typing import Any, Iterable, Optional

import pandas as pd

from trading.ranking.summary.constants import (
    DATETIME_CANDIDATES,
    RANKING_SNAPSHOT_TABLE,
)
from trading.ranking.summary.normalize import (
    filter_lookback,
    filter_symbols,
    filter_trade_date_if_possible,
    normalize_ranking_snapshot_df,
)
from trading.ranking.summary.utils import (
    default_ranking_db_path,
    normalize_symbols,
    normalize_trade_date,
    path_exists,
)

logger = logging.getLogger(__name__)


def read_sqlite_table(
    *,
    db_path: str,
    sql: str,
    params: Optional[dict[str, Any]] = None,
) -> pd.DataFrame:
    if not path_exists(db_path):
        logger.warning(
            "[RANKING SUMMARY RUNNER] ranking db not found path=%s",
            db_path,
        )
        return pd.DataFrame()

    conn: Optional[sqlite3.Connection] = None

    try:
        conn = sqlite3.connect(
            db_path,
            timeout=30.0,
            isolation_level=None,
        )

        try:
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA query_only=ON")
        except Exception:
            pass

        return pd.read_sql_query(sql, conn, params=params or {})

    except Exception:
        logger.exception(
            "[RANKING SUMMARY RUNNER] sqlite read failed path=%s",
            db_path,
        )
        return pd.DataFrame()

    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def read_via_sqlalchemy_engine(
    *,
    sql: str,
    params: Optional[dict[str, Any]] = None,
) -> pd.DataFrame:
    try:
        from sqlalchemy import text
        from database.session import get_ranking_engine

        engine = get_ranking_engine()
        if engine is None:
            logger.warning("[RANKING SUMMARY RUNNER] get_ranking_engine returned None")
            return pd.DataFrame()

        with engine.connect() as conn:
            return pd.read_sql_query(text(sql), conn, params=params or {})

    except Exception:
        logger.exception(
            "[RANKING SUMMARY RUNNER] sqlalchemy read failed"
        )
        return pd.DataFrame()


def table_exists_sqlite(db_path: str, table_name: str) -> bool:
    if not path_exists(db_path):
        return False

    conn: Optional[sqlite3.Connection] = None

    try:
        conn = sqlite3.connect(db_path, timeout=10.0)
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        )
        return cur.fetchone() is not None

    except Exception:
        return False

    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def get_sqlite_columns(
    *,
    db_path: str,
    table_name: str,
) -> list[str]:
    if not path_exists(db_path):
        return []

    conn: Optional[sqlite3.Connection] = None

    try:
        conn = sqlite3.connect(db_path, timeout=10.0)
        cur = conn.execute(f'PRAGMA table_info("{table_name}")')
        rows = cur.fetchall()
        return [str(r[1]) for r in rows if len(r) >= 2]

    except Exception:
        logger.exception(
            "[RANKING SUMMARY RUNNER] failed to get table columns db=%s table=%s",
            db_path,
            table_name,
        )
        return []

    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def resolve_sql_order_column(
    *,
    db_path: str,
    table_name: str,
) -> Optional[str]:
    cols = get_sqlite_columns(
        db_path=db_path,
        table_name=table_name,
    )

    if not cols:
        return None

    for c in DATETIME_CANDIDATES:
        if c in cols:
            return c

    return None


def load_ranking_snapshot_1min(
    *,
    trade_date: Optional[str | int | dt.date | dt.datetime] = None,
    lookback_minutes: int = 240,
    symbols: Optional[Iterable[str]] = None,
    ranking_db_path: Optional[str] = None,
) -> pd.DataFrame:
    """
    ranking_snapshot_1min からランキング由来 summary の元データを読む。
    """
    d = normalize_trade_date(trade_date)
    db_path = ranking_db_path or default_ranking_db_path(d)

    symbol_list = normalize_symbols(symbols)

    where: list[str] = []
    params: dict[str, Any] = {}

    sql = f"""
        SELECT *
        FROM {RANKING_SNAPSHOT_TABLE}
    """

    if symbol_list:
        placeholders = []

        for i, sym in enumerate(symbol_list):
            key = f"sym_{i}"
            placeholders.append(f":{key}")
            params[key] = sym

        where.append(f"CAST(symbol AS TEXT) IN ({','.join(placeholders)})")

    if where:
        sql += "\nWHERE " + " AND ".join(where)

    raw = pd.DataFrame()

    if ranking_db_path or path_exists(db_path):
        if not table_exists_sqlite(db_path, RANKING_SNAPSHOT_TABLE):
            logger.warning(
                "[RANKING SUMMARY RUNNER] table not found db=%s table=%s",
                db_path,
                RANKING_SNAPSHOT_TABLE,
            )
            return pd.DataFrame()

        order_col = resolve_sql_order_column(
            db_path=db_path,
            table_name=RANKING_SNAPSHOT_TABLE,
        )

        sql_with_order = sql

        if order_col:
            sql_with_order += f'\nORDER BY "{order_col}" ASC'
        else:
            logger.warning(
                "[RANKING SUMMARY RUNNER] no sql order column found table=%s db=%s",
                RANKING_SNAPSHOT_TABLE,
                db_path,
            )

        raw = read_sqlite_table(
            db_path=db_path,
            sql=sql_with_order,
            params=params,
        )

        if raw.empty and symbol_list:
            logger.warning(
                "[RANKING SUMMARY RUNNER] SQL symbol filter returned empty -> retry without symbol filter db=%s symbols=%s",
                db_path,
                len(symbol_list),
            )

            sql_retry = f"""
                SELECT *
                FROM {RANKING_SNAPSHOT_TABLE}
            """

            if order_col:
                sql_retry += f'\nORDER BY "{order_col}" ASC'

            raw = read_sqlite_table(
                db_path=db_path,
                sql=sql_retry,
                params={},
            )

    else:
        raw = read_via_sqlalchemy_engine(
            sql=sql,
            params=params,
        )

        if raw.empty and symbol_list:
            logger.warning(
                "[RANKING SUMMARY RUNNER] SQLAlchemy symbol filter returned empty -> retry without symbol filter"
            )
            raw = read_via_sqlalchemy_engine(
                sql=f"""
                    SELECT *
                    FROM {RANKING_SNAPSHOT_TABLE}
                """,
                params={},
            )

    if raw is None or raw.empty:
        logger.warning(
            "[RANKING SUMMARY RUNNER] ranking snapshot empty trade_date=%s db=%s",
            d,
            db_path,
        )
        return pd.DataFrame()

    logger.info(
        "[RANKING SUMMARY RUNNER] snapshot loaded rows=%s cols=%s db=%s",
        len(raw),
        list(raw.columns),
        db_path,
    )

    df = normalize_ranking_snapshot_df(raw)

    if df.empty:
        logger.warning(
            "[RANKING SUMMARY RUNNER] snapshot normalized empty rows_raw=%s cols=%s",
            len(raw),
            list(raw.columns),
        )
        return df

    df = filter_trade_date_if_possible(df, trade_date=d)
    df = filter_symbols(df, symbol_list, fallback_if_empty=True)
    df = filter_lookback(df, lookback_minutes=lookback_minutes)

    if df.empty:
        logger.warning(
            "[RANKING SUMMARY RUNNER] snapshot prepared empty after filters "
            "trade_date=%s lookback=%s symbols=%s",
            d,
            lookback_minutes,
            len(symbol_list) if symbol_list else 0,
        )
        return df

    logger.info(
        "[RANKING SUMMARY RUNNER] snapshot prepared rows=%s symbols=%s "
        "dt_min=%s dt_max=%s close_nonnull=%s close_gt0=%s types=%s",
        len(df),
        df["symbol"].nunique() if "symbol" in df.columns and not df.empty else 0,
        df["datetime"].min() if "datetime" in df.columns and not df.empty else None,
        df["datetime"].max() if "datetime" in df.columns and not df.empty else None,
        int(df["close"].notna().sum()) if "close" in df.columns else 0,
        int((pd.to_numeric(df["close"], errors="coerce") > 0).sum()) if "close" in df.columns else 0,
        df["ranking_type"].value_counts().head(10).to_dict() if "ranking_type" in df.columns else {},
    )

    return df.reset_index(drop=True)