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

import contextlib
import json
import logging
import os
import sqlite3
import threading
from datetime import datetime
from typing import Any
from nas_root import nas_path, nas_root

logger = logging.getLogger(__name__)

BASE_DIR = nas_path(r'raw_data\kabu_station\runtime_state')


# ============================================================
# helpers
# ============================================================

def _today() -> str:
    return datetime.now().strftime('%Y%m%d')


def get_runtime_state_db_path(trade_date: str | None = None) -> str:
    td = trade_date or _today()
    os.makedirs(BASE_DIR, exist_ok=True)
    return os.path.join(BASE_DIR, f'runtime_state_{td}.db')


# P0-1: 接続リーク対策。
#
# 旧実装は全関数が `with _connect(path) as conn:` の形で、close() がどこにも
# 無かった。`with sqlite3.Connection` はトランザクションを commit/rollback
# するだけで接続は閉じない (Python 公式仕様)。参照が traceback / cursor /
# logger.exception のフレームに残ると接続は GC まで生き残り、
# 実測で 1 接続あたり Win32 ハンドルを 3 個保持し続ける
# (tests: 500 接続で +1502 handles、明示 close で +0)。
#
# BASE_DIR は NAS(SMB) なので、これが積み上がると SMB のオープン上限に当たり
#   - sqlite3.OperationalError: unable to open database file (SQLITE_CANTOPEN)
#   - sqlite3.OperationalError: disk I/O error          (SQLITE_IOERR)
#   - OSError: [Errno 24] Too many open files           (ローカルパスでも発生)
# が同時多発する。2026-08-27 15:21:44 以降の positions.db 障害はこれ。
# この関数は必ず close() する。
_CONN_OPENED = 0
_CONN_CLOSED = 0
_CONN_LOCK = threading.Lock()


@contextlib.contextmanager
def _connect(path: str):
    global _CONN_OPENED, _CONN_CLOSED
    conn = sqlite3.connect(path, timeout=30)
    with _CONN_LOCK:
        _CONN_OPENED += 1
    try:
        # journal_mode は DB ファイルに永続するので毎接続で投げ直さない。
        # NAS 越しでは 1 PRAGMA が 1 往復になるため、接続あたりの
        # 往復数をそのまま SMB 負荷として削る。
        conn.execute('PRAGMA busy_timeout=30000')
        conn.execute('PRAGMA synchronous=NORMAL')
        yield conn
        try:
            conn.commit()
        except Exception:
            logger.debug('[RUNTIME STATE] commit failed path=%s', path, exc_info=True)
    except Exception:
        try:
            conn.rollback()
        except Exception:
            logger.debug('[RUNTIME STATE] rollback failed path=%s', path, exc_info=True)
        raise
    finally:
        try:
            conn.close()
        except Exception:
            logger.debug('[RUNTIME STATE] close failed path=%s', path, exc_info=True)
        else:
            with _CONN_LOCK:
                _CONN_CLOSED += 1


def connection_stats() -> dict:
    """診断用: この store が開いた/閉じた接続数。差分が増え続けたらリーク。"""
    with _CONN_LOCK:
        return {
            'opened': _CONN_OPENED,
            'closed': _CONN_CLOSED,
            'outstanding': _CONN_OPENED - _CONN_CLOSED,
        }


# schema 作成は 1 日 1 回で足りる。旧実装は save_* のたびに
# ensure_runtime_state_db() を呼んでいたため、1 回の保存で NAS へ
# 接続が 2 本走っていた (実測 [RUNTIME STATE] schema ensured x420)。
_SCHEMA_DONE: set[str] = set()
_SCHEMA_LOCK = threading.Lock()


def _ensured_path(trade_date: str | None = None) -> str:
    path = get_runtime_state_db_path(trade_date)
    with _SCHEMA_LOCK:
        done = path in _SCHEMA_DONE
    if done:
        return path
    return ensure_runtime_state_db(trade_date)


# ============================================================
# schema
# ============================================================

def ensure_runtime_state_db(trade_date: str | None = None) -> str:
    path = get_runtime_state_db_path(trade_date)

    with _SCHEMA_LOCK:
        if path in _SCHEMA_DONE:
            return path

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

    with _SCHEMA_LOCK:
        _SCHEMA_DONE.add(path)

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
    path = _ensured_path(trade_date)
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
    path = _ensured_path(trade_date)
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
    path = _ensured_path(trade_date)
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
    path = _ensured_path(trade_date)
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
    path = _ensured_path(trade_date)
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
    path = _ensured_path(trade_date)
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
    path = _ensured_path(trade_date)
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
    path = _ensured_path(trade_date)
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
    path = _ensured_path(trade_date)
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
