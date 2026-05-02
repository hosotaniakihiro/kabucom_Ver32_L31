# ============================================================
# File   : core/startup/summary_runtime_pkg/sqlite_seed.py
# Version: REV3.0-SUMMARY-RUNTIME-SQLITE-SEED
# ------------------------------------------------------------
# 【概要】
#   summary DB の SQLite direct fallback
#
# 【目的】
#   loaders_summary が存在しない / 引数差異 / symbols=None 非対応でも、
#   summary SQLite DB から 1min / 3min / 5min の履歴を直接読み込む。
#
# 【主な機能】
#   - summary DB path 解決
#   - table/columns 存在確認
#   - datetime expression 解決
#   - symbolごとの recent tail 取得
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any

import pandas as pd

from global_state import global_data

from .state import (
    DEFAULT_SUMMARY_DIR,
    SUMMARY_TABLE_BY_TF,
)
from .dataframe_utils import (
    normalize_datetime_for_tf,
    dedupe_symbol_datetime,
    tail_per_symbol,
    symbols_count,
    latest_dt_str,
)

logger = logging.getLogger(__name__)


def coerce_sqlite_path_from_urlish(v: Any) -> str:
    try:
        if v is None:
            return ""

        s = str(v).strip()
        if not s:
            return ""

        if s.startswith("sqlite:///"):
            s = s.replace("sqlite:///", "", 1)

        if s.startswith("sqlite://"):
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
    candidates = []

    try:
        import database.session as session_mod

        for attr in (
            "summary_engine",
            "SUMMARY_ENGINE",
            "engine_summary",
            "summary_db_engine",
        ):
            obj = getattr(session_mod, attr, None)
            if obj is not None:
                candidates.append(obj)

        for fn_name in (
            "get_summary_engine",
            "get_engine_summary",
            "summary_engine_factory",
        ):
            fn = getattr(session_mod, fn_name, None)
            if callable(fn):
                try:
                    obj = fn()
                    if obj is not None:
                        candidates.append(obj)
                except Exception:
                    logger.debug(
                        "[summary_runtime] summary engine factory failed fn=%s",
                        fn_name,
                        exc_info=True,
                    )
    except Exception:
        logger.debug("[summary_runtime] database.session import failed for summary db path", exc_info=True)

    for obj in candidates:
        try:
            url = getattr(obj, "url", None)
            if url is not None:
                database = getattr(url, "database", None)
                if database:
                    path = coerce_sqlite_path_from_urlish(database)
                    if path_exists(path):
                        return path

                path = coerce_sqlite_path_from_urlish(url)
                if path_exists(path):
                    return path

            path = coerce_sqlite_path_from_urlish(obj)
            if path_exists(path):
                return path
        except Exception:
            logger.debug("[summary_runtime] summary engine path candidate failed", exc_info=True)

    return ""


def resolve_summary_db_path_from_global_data() -> str:
    attrs = [
        "summary_db_path",
        "summary_path",
        "current_summary_db_path",
        "resolved_summary_db_path",
        "today_summary_db_path",
        "summary_db",
        "summary_sqlite_path",
    ]

    for attr in attrs:
        try:
            v = getattr(global_data, attr, None)
            path = coerce_sqlite_path_from_urlish(v)
            if path_exists(path):
                return path
        except Exception:
            pass

    return ""


def candidate_summary_dirs() -> list[str]:
    out: list[str] = []

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
        import database.session as session_mod

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

    out.append(DEFAULT_SUMMARY_DIR)

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
            logger.debug("[summary_runtime] summary dir scan failed dir=%s", d, exc_info=True)

    return ""


def resolve_summary_db_path() -> str:
    path = resolve_summary_db_path_from_engine()
    if path_exists(path):
        logger.info("[summary_runtime] DB seed summary db path resolved from engine: %s", path)
        return path

    path = resolve_summary_db_path_from_global_data()
    if path_exists(path):
        logger.info("[summary_runtime] DB seed summary db path resolved from global_data: %s", path)
        return path

    path = resolve_summary_db_path_from_standard_dir()
    if path_exists(path):
        logger.info("[summary_runtime] DB seed summary db path resolved from standard dir: %s", path)
        return path

    logger.warning("[summary_runtime] DB seed summary db path unresolved")
    return ""


def sqlite_table_exists(conn: sqlite3.Connection, table: str) -> bool:
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        return row is not None
    except Exception:
        return False


def sqlite_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    try:
        rows = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
        return [str(r[1]) for r in rows]
    except Exception:
        return []


def sqlite_datetime_expr(cols: list[str]) -> str:
    colset = set(cols)

    if "datetime" in colset:
        return "datetime"

    if "end_time" in colset:
        return "end_time"

    if "start_time" in colset:
        return "start_time"

    if "date" in colset and "time_range" in colset:
        return "date || ' ' || substr(time_range, 1, 5) || ':00'"

    if "date" in colset and "time" in colset:
        return "date || ' ' || substr(time, 1, 8)"

    if "last_update" in colset:
        return "last_update"

    return ""


def load_summary_seed_by_sqlite_direct(tf: int, *, bars_per_symbol: int) -> pd.DataFrame:
    db_path = resolve_summary_db_path()
    if not path_exists(db_path):
        logger.warning("[summary_runtime] DB seed sqlite direct skipped reason=db_not_found path=%s", db_path)
        return pd.DataFrame()

    table = SUMMARY_TABLE_BY_TF.get(int(tf), f"stock_summary_{int(tf)}min")

    conn = None

    try:
        conn = sqlite3.connect(db_path, timeout=8.0)
        conn.execute("PRAGMA busy_timeout=8000")

        if not sqlite_table_exists(conn, table):
            logger.warning(
                "[summary_runtime] DB seed sqlite direct table not found tf=%s table=%s db=%s",
                tf,
                table,
                db_path,
            )
            return pd.DataFrame()

        cols = sqlite_columns(conn, table)
        if not cols:
            logger.warning(
                "[summary_runtime] DB seed sqlite direct columns empty tf=%s table=%s",
                tf,
                table,
            )
            return pd.DataFrame()

        if "symbol" not in cols:
            logger.warning(
                "[summary_runtime] DB seed sqlite direct no symbol column tf=%s table=%s cols=%s",
                tf,
                table,
                cols,
            )
            return pd.DataFrame()

        dt_expr = sqlite_datetime_expr(cols)
        if not dt_expr:
            logger.warning(
                "[summary_runtime] DB seed sqlite direct no datetime expression tf=%s table=%s cols=%s",
                tf,
                table,
                cols,
            )
            return pd.DataFrame()

        try:
            latest_row = conn.execute(
                f'SELECT MAX(datetime({dt_expr})) FROM "{table}" WHERE {dt_expr} IS NOT NULL'
            ).fetchone()
            latest_dt = latest_row[0] if latest_row else None
        except Exception:
            latest_dt = None

        where_latest = ""
        params: list[Any] = []

        if latest_dt:
            where_latest = "WHERE datetime(__dt) <= datetime(?)"
            params.append(latest_dt)

        sql = f"""
            WITH src AS (
                SELECT
                    *,
                    datetime({dt_expr}) AS __dt
                FROM "{table}"
                WHERE {dt_expr} IS NOT NULL
                  AND symbol IS NOT NULL
                  AND TRIM(CAST(symbol AS TEXT)) != ''
            ),
            ranked AS (
                SELECT
                    *,
                    ROW_NUMBER() OVER (
                        PARTITION BY CAST(symbol AS TEXT)
                        ORDER BY datetime(__dt) DESC
                    ) AS __rn
                FROM src
                {where_latest}
            )
            SELECT *
            FROM ranked
            WHERE __rn <= ?
            ORDER BY CAST(symbol AS TEXT), datetime(__dt)
        """

        params.append(int(bars_per_symbol))

        try:
            df = pd.read_sql_query(sql, conn, params=params)
        except Exception:
            logger.debug(
                "[summary_runtime] DB seed sqlite direct window query failed -> fallback simple read tf=%s table=%s",
                tf,
                table,
                exc_info=True,
            )

            sql2 = f"""
                SELECT
                    *,
                    datetime({dt_expr}) AS __dt
                FROM "{table}"
                WHERE {dt_expr} IS NOT NULL
                  AND symbol IS NOT NULL
                  AND TRIM(CAST(symbol AS TEXT)) != ''
                ORDER BY datetime({dt_expr}) DESC
                LIMIT ?
            """
            limit_rows = max(int(bars_per_symbol) * 5000, 10000)
            df = pd.read_sql_query(sql2, conn, params=[limit_rows])

        if df is None or df.empty:
            logger.warning(
                "[summary_runtime] DB seed sqlite direct empty tf=%s table=%s db=%s",
                tf,
                table,
                db_path,
            )
            return pd.DataFrame()

        if "__dt" in df.columns:
            df["datetime"] = pd.to_datetime(df["__dt"], errors="coerce")
            df = df.drop(columns=["__dt"], errors="ignore")

        df = df.drop(columns=["__rn"], errors="ignore")

        df = normalize_datetime_for_tf(df, tf)
        df = dedupe_symbol_datetime(df)
        df = tail_per_symbol(df, bars_per_symbol)

        logger.info(
            "[summary_runtime] DB seed sqlite direct loaded tf=%s table=%s db=%s rows=%d symbols=%d latest_dt=%s bars=%d",
            tf,
            table,
            db_path,
            len(df),
            symbols_count(df),
            latest_dt_str(df),
            bars_per_symbol,
        )

        return df

    except Exception:
        logger.exception(
            "[summary_runtime] DB seed sqlite direct failed tf=%s table=%s db=%s",
            tf,
            table,
            db_path,
        )
        return pd.DataFrame()

    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass


__all__ = [
    "coerce_sqlite_path_from_urlish",
    "path_exists",
    "resolve_summary_db_path",
    "load_summary_seed_by_sqlite_direct",
]