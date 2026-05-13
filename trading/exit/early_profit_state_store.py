# ============================================================
# File   : trading/exit/early_profit_state_store.py
# Version: V1.1-PERSISTENT-STAGNATION-PROGRESS-STATE
# ------------------------------------------------------------
# early_profit_guard の runtime state をSQLiteへ保存する。
# 再起動後も entry後高値/安値/保持開始時刻/最後に有利方向へ進んだ時刻を復元する。
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_DB = r"\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\runtime\early_profit_state.db"


def _db_path() -> str:
    p = os.getenv("EARLY_PROFIT_STATE_DB", "").strip()
    if p:
        return p
    return _DEFAULT_DB


def _ensure_parent(path: str) -> None:
    try:
        parent = Path(path).parent
        parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass


def _connect() -> sqlite3.Connection:
    path = _db_path()
    _ensure_parent(path)
    conn = sqlite3.connect(path, timeout=3.0)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=3000")
    except Exception:
        pass
    return conn


def _iso(v: Any) -> str:
    if isinstance(v, dt.datetime):
        return v.replace(tzinfo=None).isoformat(sep=" ", timespec="seconds")
    try:
        return str(v or "").strip()
    except Exception:
        return ""


def _parse_time(v: Any) -> dt.datetime | None:
    if isinstance(v, dt.datetime):
        return v.replace(tzinfo=None) if v.tzinfo else v
    try:
        s = str(v or "").strip()
        if not s:
            return None
        s = s.replace("T", " ").split("+", 1)[0].rstrip("Z")
        return dt.datetime.fromisoformat(s)
    except Exception:
        return None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except Exception:
        return set()


def _ensure_column(conn: sqlite3.Connection, table: str, name: str, ddl_type: str) -> None:
    cols = _columns(conn, table)
    if name in cols:
        return
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl_type}")


def ensure_schema() -> None:
    try:
        with _connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS early_profit_state (
                    state_key TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    high_after_entry REAL NOT NULL DEFAULT 0,
                    low_after_entry REAL NOT NULL DEFAULT 0,
                    started_at TEXT,
                    updated_at TEXT
                )
                """
            )
            _ensure_column(conn, "early_profit_state", "last_progress_at", "TEXT")
            _ensure_column(conn, "early_profit_state", "last_progress_price", "REAL NOT NULL DEFAULT 0")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_early_profit_state_symbol ON early_profit_state(symbol, side)"
            )
    except Exception:
        logger.exception("[EARLY PROFIT STATE] ensure_schema failed path=%s", _db_path())


def load_state(state_key: str) -> dict[str, Any] | None:
    try:
        ensure_schema()
        with _connect() as conn:
            row = conn.execute(
                """
                SELECT state_key, symbol, side, entry_price,
                       high_after_entry, low_after_entry,
                       started_at, updated_at,
                       last_progress_at, last_progress_price
                  FROM early_profit_state
                 WHERE state_key = ?
                """,
                (state_key,),
            ).fetchone()
        if not row:
            return None
        return {
            "state_key": row[0],
            "symbol": row[1],
            "side": row[2],
            "entry_price": float(row[3] or 0),
            "high": float(row[4] or 0),
            "low": float(row[5] or 0),
            "started_at": _parse_time(row[6]),
            "updated_at": _parse_time(row[7]),
            "last_progress_at": _parse_time(row[8]),
            "last_progress_price": float(row[9] or 0),
        }
    except Exception:
        logger.exception("[EARLY PROFIT STATE] load failed key=%s", state_key)
        return None


def save_state(
    *,
    state_key: str,
    symbol: str,
    side: str,
    entry_price: float,
    high_after_entry: float,
    low_after_entry: float,
    started_at: dt.datetime | None,
    updated_at: dt.datetime | None,
    last_progress_at: dt.datetime | None = None,
    last_progress_price: float = 0.0,
) -> None:
    try:
        ensure_schema()
        with _connect() as conn:
            conn.execute(
                """
                INSERT INTO early_profit_state (
                    state_key, symbol, side, entry_price,
                    high_after_entry, low_after_entry,
                    started_at, updated_at,
                    last_progress_at, last_progress_price
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(state_key) DO UPDATE SET
                    symbol = excluded.symbol,
                    side = excluded.side,
                    entry_price = excluded.entry_price,
                    high_after_entry = excluded.high_after_entry,
                    low_after_entry = excluded.low_after_entry,
                    started_at = excluded.started_at,
                    updated_at = excluded.updated_at,
                    last_progress_at = excluded.last_progress_at,
                    last_progress_price = excluded.last_progress_price
                """,
                (
                    state_key,
                    str(symbol),
                    str(side),
                    float(entry_price or 0),
                    float(high_after_entry or 0),
                    float(low_after_entry or 0),
                    _iso(started_at),
                    _iso(updated_at),
                    _iso(last_progress_at),
                    float(last_progress_price or 0),
                ),
            )
    except Exception:
        logger.exception("[EARLY PROFIT STATE] save failed key=%s symbol=%s", state_key, symbol)


def delete_state(state_key: str) -> None:
    try:
        ensure_schema()
        with _connect() as conn:
            conn.execute("DELETE FROM early_profit_state WHERE state_key = ?", (state_key,))
    except Exception:
        logger.exception("[EARLY PROFIT STATE] delete failed key=%s", state_key)


__all__ = ["load_state", "save_state", "delete_state", "ensure_schema"]
