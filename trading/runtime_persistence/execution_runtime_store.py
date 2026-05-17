# ============================================================
# File   : trading/runtime_persistence/execution_runtime_store.py
# Version: Ver01-EXECUTION-RUNTIME-STORE
# ------------------------------------------------------------
# 約定イベント保存用。
# Kabu API の約定照会結果、または注文レスポンスに含まれる約定情報を
# runtime_state_YYYYMMDD.db の executions_runtime に保存する。
#
# このモジュールは注文を出さない。保存のみ。
# ============================================================

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from typing import Any

from .runtime_state_store import get_runtime_state_db_path, mark_pending_order_done, save_position_state

logger = logging.getLogger(__name__)


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA synchronous=NORMAL')
    return conn


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == '':
            return default
        return float(v)
    except Exception:
        return default


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        if v is None or v == '':
            return default
        return int(float(v))
    except Exception:
        return default


def _get(d: dict, *keys: str, default=None):
    if not isinstance(d, dict):
        return default
    for k in keys:
        if k in d and d.get(k) not in (None, ''):
            return d.get(k)
    return default


def ensure_execution_runtime_db(trade_date: str | None = None) -> str:
    path = get_runtime_state_db_path(trade_date)
    with _connect(path) as conn:
        conn.execute(
            '''
            CREATE TABLE IF NOT EXISTS executions_runtime (
                execution_id TEXT PRIMARY KEY,
                order_id TEXT,
                symbol TEXT,
                side TEXT,
                qty INTEGER,
                price REAL,
                execution_time TEXT,
                event_type TEXT,
                status TEXT,
                pnl REAL,
                source TEXT,
                payload_json TEXT,
                created_at TEXT
            )
            '''
        )
        conn.execute('CREATE INDEX IF NOT EXISTS idx_executions_runtime_order_id ON executions_runtime(order_id)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_executions_runtime_symbol_time ON executions_runtime(symbol, execution_time)')
        conn.commit()
    return path


def save_execution_event(
    *,
    execution_id: str | None = None,
    order_id: str | None = None,
    symbol: str,
    side: str,
    qty: Any,
    price: Any,
    execution_time: Any = None,
    event_type: str = 'EXECUTION',
    status: str = 'FILLED',
    pnl: Any = 0.0,
    source: str = 'unknown',
    payload: dict | None = None,
    trade_date: str | None = None,
) -> str:
    """約定イベントを1件保存する。"""
    path = ensure_execution_runtime_db(trade_date)
    now = datetime.now().isoformat(timespec='seconds')
    exec_time = execution_time or now
    exec_time_s = exec_time.isoformat(timespec='seconds') if isinstance(exec_time, datetime) else str(exec_time)

    if not execution_id:
        execution_id = f'{order_id or "NO_ORDER"}:{symbol}:{side}:{exec_time_s}:{qty}:{price}:{event_type}'

    with _connect(path) as conn:
        conn.execute(
            '''
            INSERT OR REPLACE INTO executions_runtime (
                execution_id,
                order_id,
                symbol,
                side,
                qty,
                price,
                execution_time,
                event_type,
                status,
                pnl,
                source,
                payload_json,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            (
                str(execution_id),
                str(order_id or ''),
                str(symbol),
                str(side),
                _safe_int(qty, 0),
                _safe_float(price, 0.0),
                exec_time_s,
                str(event_type),
                str(status),
                _safe_float(pnl, 0.0),
                str(source),
                json.dumps(payload or {}, ensure_ascii=False, default=str),
                now,
            ),
        )
        conn.commit()

    if order_id:
        try:
            mark_pending_order_done(str(order_id), status=str(status), trade_date=trade_date)
        except Exception:
            logger.debug('[EXECUTION RUNTIME] mark pending done skipped order_id=%s', order_id, exc_info=True)

    logger.warning(
        '[EXECUTION RUNTIME] saved execution_id=%s order_id=%s symbol=%s side=%s qty=%s price=%s status=%s source=%s',
        execution_id,
        order_id,
        symbol,
        side,
        qty,
        price,
        status,
        source,
    )
    return str(execution_id)


def normalize_kabu_execution(raw: dict) -> dict:
    """Kabu API 約定/注文照会のキー揺れを吸収する。"""
    order_id = str(_get(raw, 'OrderId', 'order_id', 'ID', 'id', default='') or '')
    execution_id = str(_get(raw, 'ExecutionID', 'ExecutionId', 'execution_id', 'ExecutionNo', default='') or '')
    symbol = str(_get(raw, 'Symbol', 'symbol', 'Code', 'code', default='') or '')

    side_raw = str(_get(raw, 'Side', 'side', 'BuySell', 'buy_sell', default='') or '').upper()
    if side_raw == '2' or side_raw.startswith('BUY') or side_raw == '買':
        side = 'BUY'
    elif side_raw == '1' or side_raw.startswith('SELL') or side_raw.startswith('SHORT') or side_raw == '売':
        side = 'SELL'
    else:
        side = side_raw

    qty = _safe_int(_get(raw, 'Qty', 'ExecutionQty', 'CumQty', 'qty', 'quantity', default=0), 0)
    price = _safe_float(_get(raw, 'Price', 'ExecutionPrice', 'AvgPrice', 'price', default=0.0), 0.0)
    execution_time = _get(raw, 'ExecutionTime', 'ExecutionDay', 'DateTime', 'datetime', 'time', default=datetime.now().isoformat(timespec='seconds'))
    status = str(_get(raw, 'State', 'Status', 'status', default='FILLED') or 'FILLED')

    if not execution_id:
        execution_id = f'{order_id}:{symbol}:{side}:{execution_time}:{qty}:{price}'

    return {
        'execution_id': execution_id,
        'order_id': order_id,
        'symbol': symbol,
        'side': side,
        'qty': qty,
        'price': price,
        'execution_time': execution_time,
        'status': status,
        'payload': raw,
    }


def save_kabu_execution(raw: dict, *, source: str = 'kabu_execution_query', trade_date: str | None = None) -> str | None:
    try:
        e = normalize_kabu_execution(raw)
        if not e.get('symbol') or _safe_int(e.get('qty'), 0) <= 0:
            logger.warning('[EXECUTION RUNTIME] skip invalid execution raw=%s normalized=%s', raw, e)
            return None
        return save_execution_event(
            execution_id=e.get('execution_id'),
            order_id=e.get('order_id'),
            symbol=e.get('symbol'),
            side=e.get('side'),
            qty=e.get('qty'),
            price=e.get('price'),
            execution_time=e.get('execution_time'),
            event_type='EXECUTION',
            status=e.get('status') or 'FILLED',
            source=source,
            payload=e.get('payload') or raw,
            trade_date=trade_date,
        )
    except Exception:
        logger.exception('[EXECUTION RUNTIME] save_kabu_execution failed raw=%s', raw)
        return None


def save_kabu_executions(rows: list[dict] | None, *, source: str = 'kabu_execution_query', trade_date: str | None = None) -> dict:
    rows = rows or []
    saved = []
    failed = 0
    for r in rows:
        try:
            x = save_kabu_execution(r, source=source, trade_date=trade_date)
            if x:
                saved.append(x)
            else:
                failed += 1
        except Exception:
            failed += 1
            logger.exception('[EXECUTION RUNTIME] row save failed')

    return {
        'rows': len(rows),
        'saved': len(saved),
        'failed': failed,
        'execution_ids': saved,
    }


def load_executions(order_id: str | None = None, symbol: str | None = None, trade_date: str | None = None) -> list[dict]:
    path = ensure_execution_runtime_db(trade_date)
    where = []
    params = []
    if order_id:
        where.append('order_id=?')
        params.append(str(order_id))
    if symbol:
        where.append('symbol=?')
        params.append(str(symbol))
    sql = 'SELECT * FROM executions_runtime'
    if where:
        sql += ' WHERE ' + ' AND '.join(where)
    sql += ' ORDER BY execution_time DESC'

    with _connect(path) as conn:
        cur = conn.execute(sql, params)
        cols = [x[0] for x in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
