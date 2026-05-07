# ============================================================
# File   : data_collectors/db_prepare.py
# Version: DATA-COLLECTORS-DB-PREPARE-V1
# ------------------------------------------------------------
# Purpose:
#   - 当日DBを作成し、最低限のテーブル・INDEX・WALを準備する
#   - 既存データは削除しない
# ============================================================

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from data_collectors.config import (
    SQLITE_BUSY_TIMEOUT_MS,
    SQLITE_TIMEOUT_SEC,
    push_db_path,
    ranking_db_path,
    summary_db_path,
    subscription_db_path,
    trade_date_yyyymmdd,
)

logger = logging.getLogger(__name__)


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=SQLITE_TIMEOUT_SEC)
    conn.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA temp_store=MEMORY")
    return conn


def _ensure_push_db(path: Path) -> None:
    with _connect(path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS push_raw_1min (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                symbolname TEXT,
                datetime TEXT NOT NULL,
                price REAL,
                current_price REAL,
                volume REAL,
                trading_volume REAL,
                source TEXT DEFAULT 'push',
                inserted_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_push_raw_1min_symbol_datetime
            ON push_raw_1min(symbol, datetime)
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS push_ticks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                symbolname TEXT,
                datetime TEXT NOT NULL,
                price REAL,
                current_price REAL,
                volume REAL,
                trading_volume REAL,
                raw_json TEXT,
                inserted_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_push_ticks_symbol_datetime
            ON push_ticks(symbol, datetime)
        """)
    logger.info("[DB PREPARE] push db ready: %s", path)


def _ensure_ranking_db(path: Path) -> None:
    with _connect(path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ranking_snapshot_1min (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                symbolname TEXT,
                rank INTEGER,
                rank_type TEXT NOT NULL DEFAULT '',
                market TEXT NOT NULL DEFAULT 'ALL',
                category TEXT,
                ranking_type TEXT,
                price REAL,
                current_price REAL,
                change_rate REAL,
                change_percentage REAL,
                change_ratio REAL,
                volume REAL,
                trading_volume REAL,
                turnover REAL,
                trading_value REAL,
                tick_count REAL,
                snapshot_time TEXT NOT NULL,
                datetime TEXT NOT NULL,
                inserted_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS ux_ranking_snapshot_1min_key
            ON ranking_snapshot_1min(symbol, datetime, rank_type, market)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_ranking_snapshot_1min_datetime
            ON ranking_snapshot_1min(datetime)
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS ranking_raw_1min (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                symbolname TEXT,
                rank INTEGER,
                rank_type TEXT,
                market TEXT,
                category TEXT,
                ranking_type TEXT,
                price REAL,
                current_price REAL,
                change_rate REAL,
                change_percentage REAL,
                change_ratio REAL,
                volume REAL,
                trading_volume REAL,
                turnover REAL,
                trading_value REAL,
                tick_count REAL,
                snapshot_time TEXT,
                datetime TEXT,
                raw_json TEXT,
                inserted_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_ranking_raw_1min_datetime
            ON ranking_raw_1min(datetime)
        """)
    logger.info("[DB PREPARE] ranking db ready: %s", path)


def _ensure_summary_db(path: Path) -> None:
    with _connect(path) as conn:
        for table in ("stock_summary_1min", "stock_summary_3min", "stock_summary_5min"):
            conn.execute(f"""
                CREATE TABLE IF NOT EXISTS {table} (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    symbolname TEXT,
                    datetime TEXT NOT NULL,
                    date TEXT,
                    time TEXT,
                    interval INTEGER,
                    source TEXT,
                    open REAL,
                    high REAL,
                    low REAL,
                    close REAL,
                    volume REAL,
                    ma5 REAL,
                    ma25 REAL,
                    ma75 REAL,
                    rsi REAL,
                    macd REAL,
                    signal REAL,
                    atr REAL,
                    slope REAL,
                    slope_atr_scaled REAL,
                    score REAL,
                    score_buy REAL,
                    score_sell REAL,
                    score_total REAL,
                    final_score REAL,
                    display_score REAL,
                    inserted_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute(f"""
                CREATE UNIQUE INDEX IF NOT EXISTS ux_{table}_symbol_datetime
                ON {table}(symbol, datetime)
            """)
            conn.execute(f"""
                CREATE INDEX IF NOT EXISTS idx_{table}_datetime
                ON {table}(datetime)
            """)
    logger.info("[DB PREPARE] summary db ready: %s", path)


def _ensure_subscription_db(path: Path) -> None:
    with _connect(path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS subscription_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                label TEXT,
                reason TEXT,
                symbol TEXT NOT NULL,
                symbolname TEXT,
                registered INTEGER DEFAULT 1,
                datetime TEXT NOT NULL,
                inserted_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_subscription_history_datetime
            ON subscription_history(datetime)
        """)
    logger.info("[DB PREPARE] subscription db ready: %s", path)


def prepare_all_daily_dbs(trade_date: str | None = None) -> None:
    d = trade_date or trade_date_yyyymmdd()
    logger.info("[DB PREPARE] start trade_date=%s", d)

    _ensure_push_db(push_db_path(d))
    _ensure_ranking_db(ranking_db_path(d))
    _ensure_summary_db(summary_db_path(d))
    _ensure_subscription_db(subscription_db_path(d))

    logger.info("[DB PREPARE] done trade_date=%s", d)
