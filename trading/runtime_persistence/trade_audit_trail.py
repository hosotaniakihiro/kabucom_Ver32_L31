# ============================================================
# File   : trading/runtime_persistence/trade_audit_trail.py
# Version: Ver01-TRADE-AUDIT-TRAIL
# ------------------------------------------------------------
# ENTRY / EXIT の判断理由を保存する監査DB。
#
# 保存するもの:
#   - なぜENTRYしたか / なぜSKIPしたか
#   - AI confidence / score / spread / quality / imbalance / 5秒足momentum
#   - なぜEXITしたか / exit reason / pnl
#
# このモジュールは注文を出さない。保存のみ。
# ============================================================

from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

BASE_DIR = r'\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\trade_audit'


def _today() -> str:
    return datetime.now().strftime('%Y%m%d')


def get_trade_audit_db_path(trade_date: str | None = None) -> str:
    td = trade_date or _today()
    os.makedirs(BASE_DIR, exist_ok=True)
    return os.path.join(BASE_DIR, f'trade_audit_{td}.db')


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


def ensure_trade_audit_db(trade_date: str | None = None) -> str:
    path = get_trade_audit_db_path(trade_date)
    with _connect(path) as conn:
        conn.execute(
            '''
            CREATE TABLE IF NOT EXISTS trade_audit_events (
                audit_id TEXT PRIMARY KEY,
                event_time TEXT,
                event_type TEXT,
                symbol TEXT,
                side TEXT,
                action TEXT,
                source TEXT,
                interval INTEGER,
                order_id TEXT,
                qty INTEGER,
                price REAL,
                reference_price REAL,
                score REAL,
                score_buy REAL,
                score_sell REAL,
                score_total REAL,
                ai_confidence REAL,
                ai_reason TEXT,
                quality_score REAL,
                spread_pct REAL,
                spread_yen REAL,
                imbalance_ratio REAL,
                momentum_pct REAL,
                exit_reason TEXT,
                pnl REAL,
                decision TEXT,
                skip_reason TEXT,
                payload_json TEXT,
                created_at TEXT
            )
            '''
        )
        conn.execute('CREATE INDEX IF NOT EXISTS idx_trade_audit_symbol_time ON trade_audit_events(symbol, event_time)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_trade_audit_event_type ON trade_audit_events(event_type)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_trade_audit_order_id ON trade_audit_events(order_id)')
        conn.commit()
    return path


def _get(d: dict | None, *keys: str, default=None):
    if not isinstance(d, dict):
        return default
    for k in keys:
        if k in d and d.get(k) not in (None, ''):
            return d.get(k)
    return default


def save_trade_audit_event(
    *,
    event_type: str,
    symbol: str,
    side: str = '',
    action: str = '',
    source: str = '',
    interval: Any = 0,
    order_id: str = '',
    qty: Any = 0,
    price: Any = 0.0,
    reference_price: Any = 0.0,
    score: Any = 0.0,
    score_buy: Any = 0.0,
    score_sell: Any = 0.0,
    score_total: Any = 0.0,
    ai_confidence: Any = 0.0,
    ai_reason: str = '',
    quality_score: Any = 0.0,
    spread_pct: Any = 0.0,
    spread_yen: Any = 0.0,
    imbalance_ratio: Any = 0.0,
    momentum_pct: Any = 0.0,
    exit_reason: str = '',
    pnl: Any = 0.0,
    decision: str = '',
    skip_reason: str = '',
    payload: dict | None = None,
    event_time: Any = None,
    trade_date: str | None = None,
) -> str:
    path = ensure_trade_audit_db(trade_date)
    now = datetime.now().isoformat(timespec='seconds')
    event_time_s = event_time.isoformat(timespec='seconds') if isinstance(event_time, datetime) else str(event_time or now)
    audit_id = f'{event_time_s}:{event_type}:{symbol}:{side}:{action}:{order_id}:{decision}:{skip_reason}'

    with _connect(path) as conn:
        conn.execute(
            '''
            INSERT OR REPLACE INTO trade_audit_events (
                audit_id, event_time, event_type, symbol, side, action, source, interval,
                order_id, qty, price, reference_price,
                score, score_buy, score_sell, score_total,
                ai_confidence, ai_reason, quality_score,
                spread_pct, spread_yen, imbalance_ratio, momentum_pct,
                exit_reason, pnl, decision, skip_reason,
                payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            (
                audit_id,
                event_time_s,
                str(event_type),
                str(symbol),
                str(side),
                str(action),
                str(source),
                _safe_int(interval, 0),
                str(order_id or ''),
                _safe_int(qty, 0),
                _safe_float(price, 0.0),
                _safe_float(reference_price, 0.0),
                _safe_float(score, 0.0),
                _safe_float(score_buy, 0.0),
                _safe_float(score_sell, 0.0),
                _safe_float(score_total, 0.0),
                _safe_float(ai_confidence, 0.0),
                str(ai_reason or ''),
                _safe_float(quality_score, 0.0),
                _safe_float(spread_pct, 0.0),
                _safe_float(spread_yen, 0.0),
                _safe_float(imbalance_ratio, 0.0),
                _safe_float(momentum_pct, 0.0),
                str(exit_reason or ''),
                _safe_float(pnl, 0.0),
                str(decision or ''),
                str(skip_reason or ''),
                json.dumps(payload or {}, ensure_ascii=False, default=str),
                now,
            ),
        )
        conn.commit()

    logger.info('[TRADE AUDIT] saved event_type=%s symbol=%s side=%s decision=%s reason=%s', event_type, symbol, side, decision, skip_reason or exit_reason)
    return audit_id


def save_entry_decision(
    *,
    symbol: str,
    side: str,
    decision: str,
    row: dict | None = None,
    ai: dict | None = None,
    quality: dict | None = None,
    spread: dict | None = None,
    imbalance: dict | None = None,
    timing: dict | None = None,
    order_id: str = '',
    qty: Any = 0,
    price: Any = 0.0,
    skip_reason: str = '',
    payload: dict | None = None,
    trade_date: str | None = None,
) -> str:
    row = row or {}
    ai = ai or {}
    quality = quality or {}
    spread = spread or {}
    imbalance = imbalance or {}
    timing = timing or {}

    merged_payload = {
        'row': row,
        'ai': ai,
        'quality': quality,
        'spread': spread,
        'imbalance': imbalance,
        'timing': timing,
        'payload': payload or {},
    }

    return save_trade_audit_event(
        event_type='ENTRY_DECISION',
        symbol=symbol,
        side=side,
        action='ENTRY',
        source=str(_get(row, 'source', 'entry_source', default='')),
        interval=_get(row, 'interval', 'timeframe', default=0),
        order_id=order_id,
        qty=qty or _get(row, 'qty', 'Qty', default=0),
        price=price or _get(row, 'price', 'close', 'close_price', 'current_price', default=0.0),
        reference_price=_get(row, 'close_price', 'close', 'reference_price', default=0.0),
        score=_get(row, 'score', 'final_score', default=0.0),
        score_buy=_get(row, 'score_buy', 'buy_score', default=0.0),
        score_sell=_get(row, 'score_sell', 'sell_score', default=0.0),
        score_total=_get(row, 'score_total', 'total_score', default=0.0),
        ai_confidence=_get(ai, 'confidence', 'conf', 'ai_confidence', default=0.0),
        ai_reason=str(_get(ai, 'reason', 'ai_reason', default='')),
        quality_score=_get(quality, 'score', 'quality_score', default=0.0),
        spread_pct=_get(spread, 'spread_pct', default=_get(quality.get('spread') if isinstance(quality.get('spread'), dict) else {}, 'spread_pct', default=0.0)),
        spread_yen=_get(spread, 'spread', 'spread_yen', default=_get(quality.get('spread') if isinstance(quality.get('spread'), dict) else {}, 'spread', default=0.0)),
        imbalance_ratio=_get(imbalance, 'bid_ask_ratio', 'ask_bid_ratio', 'imbalance_ratio', default=0.0),
        momentum_pct=_get(timing, 'momentum_pct', default=0.0),
        decision=decision,
        skip_reason=skip_reason,
        payload=merged_payload,
        trade_date=trade_date,
    )


def save_exit_decision(
    *,
    symbol: str,
    side: str,
    exit_reason: str,
    decision: str = 'EXIT',
    order_id: str = '',
    qty: Any = 0,
    price: Any = 0.0,
    pnl: Any = 0.0,
    position: dict | None = None,
    payload: dict | None = None,
    trade_date: str | None = None,
) -> str:
    return save_trade_audit_event(
        event_type='EXIT_DECISION',
        symbol=symbol,
        side=side,
        action='EXIT',
        order_id=order_id,
        qty=qty,
        price=price,
        exit_reason=exit_reason,
        pnl=pnl,
        decision=decision,
        payload={'position': position or {}, 'payload': payload or {}},
        trade_date=trade_date,
    )


def load_trade_audit_events(symbol: str | None = None, event_type: str | None = None, trade_date: str | None = None) -> list[dict]:
    path = ensure_trade_audit_db(trade_date)
    where = []
    params = []
    if symbol:
        where.append('symbol=?')
        params.append(str(symbol))
    if event_type:
        where.append('event_type=?')
        params.append(str(event_type))

    sql = 'SELECT * FROM trade_audit_events'
    if where:
        sql += ' WHERE ' + ' AND '.join(where)
    sql += ' ORDER BY event_time DESC'

    with _connect(path) as conn:
        cur = conn.execute(sql, params)
        cols = [x[0] for x in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
