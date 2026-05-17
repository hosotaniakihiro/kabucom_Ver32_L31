# ============================================================
# File   : trading/runtime_persistence/runtime_state_store.py
# Version: Ver01-RUNTIME-STATE-STORE
# ------------------------------------------------------------
# 日中停止・再起動対策。
# runtime state を sqlite へ保存し、再起動後に復元できるようにする。
#
# 保存対象:
#   - open positions
#   - highest/lowest since entry
#   - pending orders
#   - smart entry state
#   - portfolio runtime
# ============================================================

from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

BASE_DIR = r'\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\runtime_state'


# ============================================================
# helpers
# ============================================================

def _today() -> str:
    return datetime.now().strftime('%Y%m%d')


def get_runtime_state_db_path(trade_date: str | None = None) -> str:
    td = trade_date or _today()
    os.makedirs(BASE_DIR, exist_ok=True)
    return os.path.join(BASE_DIR, f'runtime_state_{td}.db')


def _connect(path: str):
    conn = sqlite3.connect(path, timeout=30)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA synchronous=NORMAL')
    return conn


# ============================================================
# schema
# ============================================================

def ensure_runtime_state_db(trade_date: str | None = None) -> str:
    path = get_runtime_state_db_path(trade_date)

    with _connect(path) as conn:
        conn.execute(
            '''
            CREATE TABLE IF NOT EXISTS positions_runtime (
                symbol TEXT,
                side TEXT,
                entry_time TEXT,
                entry_price REAL,
                current_price REAL,
                highest_since_entry REAL,
                lowest_since_entry REAL,
                qty INTEGER,
                status TEXT,
                updated_at TEXT,
                PRIMARY KEY(symbol, side, entry_time)
            )
            '''
        )

        conn.execute(
            '''
            CREATE TABLE IF NOT EXISTS pending_orders_runtime (
                order_id TEXT PRIMARY KEY,
                symbol TEXT,
                side TEXT,
                send_time TEXT,
                cancel_deadline TEXT,
                qty INTEGER,
                status TEXT,
                updated_at TEXT
            )
            '''
        )

        conn.execute(
            '''
            CREATE TABLE IF NOT EXISTS smart_entry_runtime (
                symbol TEXT PRIMARY KEY,
                side TEXT,
                quality_score REAL,
                spread_pct REAL,
                momentum_pct REAL,
                imbalance_ratio REAL,
                updated_at TEXT,
                payload_json TEXT
            )
            '''
        )

        conn.execute(
            '''
            CREATE TABLE IF NOT EXISTS portfolio_runtime (
                runtime_id INTEGER PRIMARY KEY AUTOINCREMENT,
                realized_pnl REAL,
                unrealized_pnl REAL,
                drawdown REAL,
                loss_streak INTEGER,
                open_positions INTEGER,
                updated_at TEXT,
                payload_json TEXT
            )
            '''
        )

        conn.commit()

    logger.info('[RUNTIME STATE] schema ensured path=%s', path)
    return path


# ============================================================
# positions
# ============================================================

def save_position_state(
    *,
    symbol: str,
    side: str,
    entry_time: Any,
    entry_price: Any,
    current_price: Any,
    highest_since_entry: Any,
    lowest_since_entry: Any,
    qty: Any,
    status: str = 'OPEN',
    trade_date: str | None = None,
):
    path = ensure_runtime_state_db(trade_date)
    now = datetime.now().isoformat()

    with _connect(path) as conn:
        conn.execute(
            '''
            INSERT OR REPLACE INTO positions_runtime (
                symbol,
                side,
                entry_time,
                entry_price,
                current_price,
                highest_since_entry,
                lowest_since_entry,
                qty,
                status,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            (
                str(symbol),
                str(side),
                str(entry_time),
                float(entry_price or 0),
                float(current_price or 0),
                float(highest_since_entry or 0),
                float(lowest_since_entry or 0),
                int(qty or 0),
                str(status),
                now,
            ),
        )
        conn.commit()


# ============================================================
# pending orders
# ============================================================

def save_pending_order_state(
    *,
    order_id: str,
    symbol: str,
    side: str,
    send_time: Any,
    cancel_deadline: Any,
    qty: Any,
    status: str = 'PENDING',
    trade_date: str | None = None,
):
    path = ensure_runtime_state_db(trade_date)
    now = datetime.now().isoformat()

    with _connect(path) as conn:
        conn.execute(
            '''
            INSERT OR REPLACE INTO pending_orders_runtime (
                order_id,
                symbol,
                side,
                send_time,
                cancel_deadline,
                qty,
                status,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            (
                str(order_id),
                str(symbol),
                str(side),
                str(send_time),
                str(cancel_deadline),
                int(qty or 0),
                str(status),
                now,
            ),
        )
        conn.commit()


# ============================================================
# smart entry runtime
# ============================================================

def save_smart_entry_state(
    *,
    symbol: str,
    side: str,
    quality_score: Any,
    spread_pct: Any,
    momentum_pct: Any,
    imbalance_ratio: Any,
    payload: dict | None = None,
    trade_date: str | None = None,
):
    path = ensure_runtime_state_db(trade_date)
    now = datetime.now().isoformat()

    with _connect(path) as conn:
        conn.execute(
            '''
            INSERT OR REPLACE INTO smart_entry_runtime (
                symbol,
                side,
                quality_score,
                spread_pct,
                momentum_pct,
                imbalance_ratio,
                updated_at,
                payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            (
                str(symbol),
                str(side),
                float(quality_score or 0),
                float(spread_pct or 0),
                float(momentum_pct or 0),
                float(imbalance_ratio or 0),
                now,
                json.dumps(payload or {}, ensure_ascii=False),
            ),
        )
        conn.commit()


# ============================================================
# portfolio runtime
# ============================================================

def save_portfolio_state(
    *,
    realized_pnl: Any,
    unrealized_pnl: Any,
    drawdown: Any,
    loss_streak: Any,
    open_positions: Any,
    payload: dict | None = None,
    trade_date: str | None = None,
):
    path = ensure_runtime_state_db(trade_date)
    now = datetime.now().isoformat()

    with _connect(path) as conn:
        conn.execute(
            '''
            INSERT INTO portfolio_runtime (
                realized_pnl,
                unrealized_pnl,
                drawdown,
                loss_streak,
                open_positions,
                updated_at,
                payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ''',
            (
                float(realized_pnl or 0),
                float(unrealized_pnl or 0),
                float(drawdown or 0),
                int(loss_streak or 0),
                int(open_positions or 0),
                now,
                json.dumps(payload or {}, ensure_ascii=False),
            ),
        )
        conn.commit()


# ============================================================
# restore
# ============================================================

def load_open_positions(trade_date: str | None = None) -> list[dict]:
    path = ensure_runtime_state_db(trade_date)
    with _connect(path) as conn:
        cur = conn.execute(
            '''
            SELECT
                symbol,
                side,
                entry_time,
                entry_price,
                current_price,
                highest_since_entry,
                lowest_since_entry,
                qty,
                status,
                updated_at
            FROM positions_runtime
            WHERE status='OPEN'
            ORDER BY updated_at DESC
            '''
        )
        cols = [x[0] for x in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def load_pending_orders(trade_date: str | None = None) -> list[dict]:
    path = ensure_runtime_state_db(trade_date)
    with _connect(path) as conn:
        cur = conn.execute(
            '''
            SELECT
                order_id,
                symbol,
                side,
                send_time,
                cancel_deadline,
                qty,
                status,
                updated_at
            FROM pending_orders_runtime
            WHERE status='PENDING'
            ORDER BY updated_at DESC
            '''
        )
        cols = [x[0] for x in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def load_latest_portfolio_state(trade_date: str | None = None) -> dict:
    path = ensure_runtime_state_db(trade_date)
    with _connect(path) as conn:
        cur = conn.execute(
            '''
            SELECT
                runtime_id,
                realized_pnl,
                unrealized_pnl,
                drawdown,
                loss_streak,
                open_positions,
                updated_at,
                payload_json
            FROM portfolio_runtime
            ORDER BY runtime_id DESC
            LIMIT 1
            '''
        )
        row = cur.fetchone()
        if not row:
            return {}
        cols = [x[0] for x in cur.description]
        return dict(zip(cols, row))


# ============================================================
# state update
# ============================================================

def mark_position_closed(symbol: str, side: str, entry_time: Any, trade_date: str | None = None):
    path = ensure_runtime_state_db(trade_date)
    now = datetime.now().isoformat()
    with _connect(path) as conn:
        conn.execute(
            '''
            UPDATE positions_runtime
            SET status='CLOSED', updated_at=?
            WHERE symbol=? AND side=? AND entry_time=?
            ''',
            (
                now,
                str(symbol),
                str(side),
                str(entry_time),
            ),
        )
        conn.commit()


def mark_pending_order_done(order_id: str, status: str = 'DONE', trade_date: str | None = None):
    path = ensure_runtime_state_db(trade_date)
    now = datetime.now().isoformat()
    with _connect(path) as conn:
        conn.execute(
            '''
            UPDATE pending_orders_runtime
            SET status=?, updated_at=?
            WHERE order_id=?
            ''',
            (
                str(status),
                now,
                str(order_id),
            ),
        )
        conn.commit()
