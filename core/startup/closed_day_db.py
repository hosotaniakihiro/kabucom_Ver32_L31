# ============================================================
# File   : core/startup/closed_day_db.py
# Version: REV1.0-CLOSED-DAY-DB-FALLBACK
# ------------------------------------------------------------
# 【概要】
#   closed-day 表示用の summary DB fallback
#
# 【主な機能】
#   - bootstrap_database 後の summary_engine を動的取得
#   - summary_engine unavailable 時の SQLite direct fallback
#   - datetime / end_time / start_time / date+time_range 対応
#
# 【重要】
#   startup.py で from database.session import summary_engine しない。
#   古い参照を掴むと bootstrap_database() 後の rebind を見失う。
# ============================================================

from __future__ import annotations

import datetime as dt
import importlib
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from config.paths import get_path
from global_state import global_data
from core.startup.startup_runtime import resolve_summary_engine_dynamic

logger = logging.getLogger(__name__)

SUMMARY_DIR = get_path("summary_db_dir")

_INTERVAL_TABLE_MAP = {
    1: "stock_summary_1min",
    3: "stock_summary_3min",
    5: "stock_summary_5min",
}

_DEFAULT_SUMMARY_DIR = r"\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\summary"


# ============================================================
# PATH / SQLITE HELPERS
# ============================================================

def coerce_sqlite_path_from_urlish(v: Any) -> str:
    try:
        if v is None:
            return ""

        s = str(v).strip()
        if not s:
            return ""

        if s.startswith("sqlite:///"):
            s = s.replace("sqlite:///", "", 1)
        elif s.startswith("sqlite://"):
            s = s.replace("sqlite://", "", 1)

        if s.startswith("file:"):
            s = s.replace("file:", "", 1)

        s = s.strip("\"'")

        if s.startswith("/") and len(s) >= 3 and s[2] == ":":
            s = s[1:]

        return s
    except Exception:
        return ""


def path_exists(path: str) -> bool:
    try:
        return bool(path and os.path.exists(path))
    except Exception:
        return False


def resolve_summary_db_path_from_engine() -> str:
    try:
        eng = resolve_summary_engine_dynamic()
        if eng is None:
            return ""

        url = getattr(eng, "url", None)
        if url is None:
            return ""

        database = getattr(url, "database", None)
        if database:
            path = coerce_sqlite_path_from_urlish(database)
            if path_exists(path):
                return path

        path = coerce_sqlite_path_from_urlish(url)
        if path_exists(path):
            return path
    except Exception:
        logger.debug("[CLOSED DAY DB] resolve summary db path from engine failed", exc_info=True)

    return ""


def resolve_summary_db_path_from_sqlite_seed() -> str:
    try:
        mod = importlib.import_module("core.startup.summary_runtime_pkg.sqlite_seed")
        fn = getattr(mod, "resolve_summary_db_path", None)
        if callable(fn):
            path = fn()
            if path_exists(path):
                return path
    except Exception:
        logger.debug("[CLOSED DAY DB] resolve summary db path via sqlite_seed failed", exc_info=True)

    return ""


def candidate_summary_dirs() -> list[str]:
    out: list[str] = []

    try:
        if SUMMARY_DIR:
            out.append(str(SUMMARY_DIR))
    except Exception:
        pass

    try:
        for attr in (
            "summary_dir",
            "summary_db_dir",
            "resolved_summary_dir",
            "kabu_summary_dir",
        ):
            v = getattr(global_data, attr, None)
            if v:
                out.append(str(v))
    except Exception:
        pass

    try:
        session_mod = importlib.import_module("database.session")
        for attr in (
            "SUMMARY_DIR",
            "summary_dir",
            "DB_SUMMARY_DIR",
        ):
            v = getattr(session_mod, attr, None)
            if v:
                out.append(str(v))
    except Exception:
        pass

    out.append(_DEFAULT_SUMMARY_DIR)

    deduped = []
    seen = set()
    for p in out:
        p = str(p).strip()
        if not p or p in seen:
            continue
        seen.add(p)
        deduped.append(p)

    return deduped


def resolve_summary_db_path_from_standard_dir() -> str:
    today = dt.date.today()
    ymd = today.strftime("%Y%m%d")

    for d in candidate_summary_dirs():
        try:
            p = Path(d)

            today_path = p / f"summary{ymd}.db"
            if today_path.exists():
                return str(today_path)

            files = sorted(
                p.glob("summary*.db"),
                key=lambda x: x.stat().st_mtime,
                reverse=True,
            )
            for f in files:
                if f.exists():
                    return str(f)
        except Exception:
            logger.debug("[CLOSED DAY DB] summary dir scan failed dir=%s", d, exc_info=True)

    return ""


def resolve_summary_db_path() -> str:
    path = resolve_summary_db_path_from_engine()
    if path_exists(path):
        logger.info("[CLOSED DAY DB] summary db path resolved from dynamic engine: %s", path)
        return path

    path = resolve_summary_db_path_from_sqlite_seed()
    if path_exists(path):
        logger.info("[CLOSED DAY DB] summary db path resolved from sqlite_seed: %s", path)
        return path

    path = resolve_summary_db_path_from_standard_dir()
    if path_exists(path):
        logger.info("[CLOSED DAY DB] summary db path resolved from standard dir: %s", path)
        return path

    logger.warning("[CLOSED DAY DB] summary db path unresolved")
    return ""


def build_sqlite_engine_from_summary_db_path() -> Engine | None:
    path = resolve_summary_db_path()
    if not path_exists(path):
        return None

    try:
        return create_engine(
            f"sqlite:///{path}",
            connect_args={"timeout": 8.0},
            future=True,
        )
    except Exception:
        logger.exception("[CLOSED DAY DB] sqlite engine create failed path=%s", path)
        return None


# ============================================================
# TABLE / DATETIME HELPERS
# ============================================================

def read_table_columns_sqlalchemy(conn, table: str) -> set[str]:
    try:
        pragma_df = pd.read_sql(text(f'PRAGMA table_info("{table}")'), conn)
        if pragma_df.empty or "name" not in pragma_df.columns:
            return set()
        return set(pragma_df["name"].astype(str).tolist())
    except Exception:
        logger.exception("[CLOSED DAY DB] failed to read columns table=%s", table)
        return set()


def read_table_columns_sqlite(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        rows = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
        return {str(r[1]) for r in rows}
    except Exception:
        logger.exception("[CLOSED DAY DB] sqlite failed to read columns table=%s", table)
        return set()


def sqlite_table_exists(conn: sqlite3.Connection, table: str) -> bool:
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        return row is not None
    except Exception:
        return False


def datetime_expr_from_cols(cols: set[str]) -> str:
    if "datetime" in cols:
        return "datetime"
    if "end_time" in cols:
        return "end_time"
    if "start_time" in cols:
        return "start_time"
    if "date" in cols and "time_range" in cols:
        return "date || ' ' || substr(time_range, 1, 5) || ':00'"
    if "date" in cols and "time" in cols:
        return "date || ' ' || substr(time, 1, 8)"
    return ""


def normalize_closed_day_db_df(df: pd.DataFrame, interval: int) -> pd.DataFrame:
    try:
        if df is None or df.empty:
            return pd.DataFrame()

        x = df.copy()

        if "datetime" not in x.columns:
            if "end_time" in x.columns:
                x["datetime"] = x["end_time"]
            elif "start_time" in x.columns:
                x["datetime"] = x["start_time"]
            elif "date" in x.columns and "time_range" in x.columns:
                x["datetime"] = (
                    x["date"].astype(str).str.slice(0, 10)
                    + " "
                    + x["time_range"].astype(str).str.slice(0, 5)
                    + ":00"
                )
            elif "date" in x.columns and "time" in x.columns:
                x["datetime"] = (
                    x["date"].astype(str).str.slice(0, 10)
                    + " "
                    + x["time"].astype(str).str.slice(0, 8)
                )

        if "datetime" in x.columns:
            x["datetime"] = pd.to_datetime(x["datetime"], errors="coerce")

        if "symbol" in x.columns:
            x["symbol"] = x["symbol"].astype(str).str.replace(".0", "", regex=False).str.strip()

        if "date" not in x.columns and "datetime" in x.columns:
            x["date"] = x["datetime"].dt.strftime("%Y-%m-%d")

        if "time_range" not in x.columns and "datetime" in x.columns:
            x["time_range"] = x["datetime"].dt.strftime("%H:%M")

        return x.reset_index(drop=True)

    except Exception:
        logger.exception("[CLOSED DAY DB] normalize df failed interval=%s", interval)
        return df if isinstance(df, pd.DataFrame) else pd.DataFrame()


# ============================================================
# LOAD LATEST SUMMARY
# ============================================================

def get_latest_db_summary_by_sqlalchemy(interval: int, eng: Engine) -> pd.DataFrame:
    table = _INTERVAL_TABLE_MAP.get(int(interval))
    if not table or eng is None:
        return pd.DataFrame()

    try:
        with eng.connect() as conn:
            cols = read_table_columns_sqlalchemy(conn, table)

            if not cols:
                logger.warning("[CLOSED DAY DB] no columns interval=%s table=%s via sqlalchemy", interval, table)
                return pd.DataFrame()

            has_datetime = "datetime" in cols
            has_date = "date" in cols
            has_time_range = "time_range" in cols

            if has_datetime:
                sql = text(f"""
                    WITH latest_dt AS (
                        SELECT MAX(datetime) AS max_dt
                        FROM "{table}"
                        WHERE datetime IS NOT NULL
                    )
                    SELECT *
                    FROM "{table}"
                    WHERE datetime = (SELECT max_dt FROM latest_dt)
                """)
                df = pd.read_sql(sql, conn)
                df = normalize_closed_day_db_df(df, interval)

                latest_dt = None
                if isinstance(df, pd.DataFrame) and not df.empty and "datetime" in df.columns:
                    latest_dt = str(pd.to_datetime(df["datetime"], errors="coerce").max())

                logger.info(
                    "[CLOSED DAY DB] loaded interval=%s table=%s mode=datetime via=sqlalchemy rows=%s latest_dt=%s",
                    interval,
                    table,
                    0 if df is None else len(df),
                    latest_dt,
                )
                return df if isinstance(df, pd.DataFrame) else pd.DataFrame()

            if has_date and has_time_range:
                latest_key_sql = text(f"""
                    SELECT date, time_range
                    FROM "{table}"
                    WHERE date IS NOT NULL
                      AND time_range IS NOT NULL
                    ORDER BY date DESC, time_range DESC
                    LIMIT 1
                """)
                latest_key_df = pd.read_sql(latest_key_sql, conn)

                if latest_key_df.empty:
                    logger.warning(
                        "[CLOSED DAY DB] no latest date/time_range row interval=%s table=%s via=sqlalchemy",
                        interval,
                        table,
                    )
                    return pd.DataFrame()

                latest_date = str(latest_key_df.iloc[0]["date"])
                latest_time_range = str(latest_key_df.iloc[0]["time_range"])

                sql = text(f"""
                    SELECT *
                    FROM "{table}"
                    WHERE date = :date
                      AND time_range = :time_range
                """)
                df = pd.read_sql(
                    sql,
                    conn,
                    params={"date": latest_date, "time_range": latest_time_range},
                )
                df = normalize_closed_day_db_df(df, interval)

                logger.info(
                    "[CLOSED DAY DB] loaded interval=%s table=%s mode=date_time_range via=sqlalchemy rows=%s latest_date=%s latest_time_range=%s",
                    interval,
                    table,
                    0 if df is None else len(df),
                    latest_date,
                    latest_time_range,
                )
                return df if isinstance(df, pd.DataFrame) else pd.DataFrame()

            logger.warning(
                "[CLOSED DAY DB] unsupported schema interval=%s table=%s cols=%s via=sqlalchemy",
                interval,
                table,
                sorted(cols),
            )
            return pd.DataFrame()

    except Exception:
        logger.exception(
            "[CLOSED DAY DB] sqlalchemy latest summary load failed interval=%s table=%s",
            interval,
            table,
        )
        return pd.DataFrame()


def get_latest_db_summary_by_sqlite(interval: int) -> pd.DataFrame:
    table = _INTERVAL_TABLE_MAP.get(int(interval))
    if not table:
        return pd.DataFrame()

    db_path = resolve_summary_db_path()
    if not path_exists(db_path):
        logger.warning("[CLOSED DAY DB] sqlite fallback skipped db not found interval=%s path=%s", interval, db_path)
        return pd.DataFrame()

    conn = None

    try:
        conn = sqlite3.connect(db_path, timeout=8.0)
        conn.execute("PRAGMA busy_timeout=8000")

        if not sqlite_table_exists(conn, table):
            logger.warning("[CLOSED DAY DB] sqlite table not found interval=%s table=%s db=%s", interval, table, db_path)
            return pd.DataFrame()

        cols = read_table_columns_sqlite(conn, table)
        if not cols:
            logger.warning("[CLOSED DAY DB] sqlite columns empty interval=%s table=%s", interval, table)
            return pd.DataFrame()

        dt_expr = datetime_expr_from_cols(cols)
        if not dt_expr:
            logger.warning(
                "[CLOSED DAY DB] sqlite unsupported schema interval=%s table=%s cols=%s",
                interval,
                table,
                sorted(cols),
            )
            return pd.DataFrame()

        sql = f"""
            WITH src AS (
                SELECT
                    *,
                    datetime({dt_expr}) AS __dt
                FROM "{table}"
                WHERE {dt_expr} IS NOT NULL
            ),
            latest_dt AS (
                SELECT MAX(__dt) AS max_dt FROM src
            )
            SELECT *
            FROM src
            WHERE __dt = (SELECT max_dt FROM latest_dt)
        """

        df = pd.read_sql_query(sql, conn)

        if "__dt" in df.columns:
            df["datetime"] = pd.to_datetime(df["__dt"], errors="coerce")
            df = df.drop(columns=["__dt"], errors="ignore")

        df = normalize_closed_day_db_df(df, interval)

        latest_dt = None
        if isinstance(df, pd.DataFrame) and not df.empty and "datetime" in df.columns:
            latest_dt = str(pd.to_datetime(df["datetime"], errors="coerce").max())

        logger.info(
            "[CLOSED DAY DB] loaded interval=%s table=%s mode=latest_dt via=sqlite rows=%s latest_dt=%s db=%s",
            interval,
            table,
            0 if df is None else len(df),
            latest_dt,
            db_path,
        )

        return df if isinstance(df, pd.DataFrame) else pd.DataFrame()

    except Exception:
        logger.exception("[CLOSED DAY DB] sqlite latest summary load failed interval=%s table=%s", interval, table)
        return pd.DataFrame()

    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass


def get_latest_db_summary(interval: int) -> pd.DataFrame:
    table = _INTERVAL_TABLE_MAP.get(int(interval))
    if not table:
        return pd.DataFrame()

    eng = resolve_summary_engine_dynamic()

    if eng is not None:
        df = get_latest_db_summary_by_sqlalchemy(interval, eng)
        if isinstance(df, pd.DataFrame) and not df.empty:
            return df

        logger.warning(
            "[CLOSED DAY DB] dynamic summary_engine path empty -> sqlite fallback interval=%s table=%s",
            interval,
            table,
        )
    else:
        logger.warning(
            "[CLOSED DAY DB] dynamic summary_engine unavailable -> sqlite fallback interval=%s table=%s",
            interval,
            table,
        )

    return get_latest_db_summary_by_sqlite(interval)


__all__ = [
    "coerce_sqlite_path_from_urlish",
    "path_exists",
    "resolve_summary_db_path",
    "build_sqlite_engine_from_summary_db_path",
    "normalize_closed_day_db_df",
    "get_latest_db_summary_by_sqlalchemy",
    "get_latest_db_summary_by_sqlite",
    "get_latest_db_summary",
]