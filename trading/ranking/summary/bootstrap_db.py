# ============================================================
# File   : trading/ranking/summary/bootstrap_db.py
# Version: Ver1.0-PRODUCTION-RANKING-SUMMARY-BOOTSTRAP-DB
# ------------------------------------------------------------
# 【概要】
#   ranking summary bootstrap 用 SQLite 共通処理
# ============================================================

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path

import pandas as pd

from trading.ranking.summary.bootstrap_config import (
    RANKING_SUMMARY_TABLES,
)

logger = logging.getLogger(__name__)


def ensure_parent_dir(path: str) -> None:
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        logger.exception("[RANKING SUMMARY BOOTSTRAP DB] ensure parent dir failed path=%s", path)


def connect_sqlite(path: str, *, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        uri = f"file:{path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=30)
    else:
        ensure_parent_dir(path)
        conn = sqlite3.connect(path, timeout=60)

    try:
        conn.execute("PRAGMA busy_timeout=60000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA temp_store=MEMORY")
    except Exception:
        pass

    return conn


def quote_ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    try:
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        )
        return cur.fetchone() is not None
    except Exception:
        return False


def get_latest_summary_datetime(db_path: str, interval: int) -> pd.Timestamp | None:
    table = RANKING_SUMMARY_TABLES.get(interval, f"ranking_summary_{interval}min")

    if not os.path.exists(db_path):
        return None

    try:
        with connect_sqlite(db_path, readonly=True) as conn:
            if not table_exists(conn, table):
                return None

            cur = conn.execute(f"SELECT MAX(datetime) FROM {quote_ident(table)}")
            row = cur.fetchone()

            if not row or row[0] is None:
                return None

            dt = pd.to_datetime(row[0], errors="coerce")
            if pd.isna(dt):
                return None

            return dt

    except Exception:
        logger.exception(
            "[RANKING SUMMARY BOOTSTRAP DB] get latest datetime failed interval=%s db=%s",
            interval,
            db_path,
        )
        return None


def get_latest_by_interval(db_path: str, intervals: tuple[int, ...]) -> dict[int, pd.Timestamp | None]:
    return {
        int(interval): get_latest_summary_datetime(db_path, int(interval))
        for interval in intervals
    }


def determine_load_from(
    latest_by_interval: dict[int, pd.Timestamp | None],
    *,
    lookback_minutes: int,
) -> pd.Timestamp:
    valid = [
        pd.to_datetime(v)
        for v in latest_by_interval.values()
        if v is not None and not pd.isna(v)
    ]

    if valid:
        return min(valid) - pd.Timedelta(minutes=int(lookback_minutes))

    now = pd.Timestamp.now()
    return pd.Timestamp(year=now.year, month=now.month, day=now.day)


__all__ = [
    "ensure_parent_dir",
    "connect_sqlite",
    "quote_ident",
    "table_exists",
    "get_latest_summary_datetime",
    "get_latest_by_interval",
    "determine_load_from",
]