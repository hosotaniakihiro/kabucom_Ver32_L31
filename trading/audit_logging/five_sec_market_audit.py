# ============================================================
# File   : trading/audit_logging/five_sec_market_audit.py
# Version: Ver01-FIVE-SEC-MARKET-AUDIT
# ------------------------------------------------------------
# 5秒足・板・スプレッドをバックテスト用に保存する。
# PUSH tick / 5秒足生成 / 板取得処理から呼び出す共通部品。
# 監査保存に失敗しても売買処理は止めない。
# ============================================================

from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime
from typing import Any

BASE_DIR = r'\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\audit'
_LOCK = threading.Lock()


def _today() -> str:
    return datetime.now().strftime('%Y%m%d')


def get_market_audit_db_path() -> str:
    os.makedirs(BASE_DIR, exist_ok=True)
    return os.path.join(BASE_DIR, f'market_audit_{_today()}.db')


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(get_market_audit_db_path(), timeout=30)
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


def ensure_market_audit_db() -> None:
    with _LOCK:
        conn = _connect()
        cur = conn.cursor()

        cur.execute('''
        CREATE TABLE IF NOT EXISTS five_sec_bars (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bucket_time TEXT NOT NULL,
            symbol TEXT NOT NULL,
            open REAL,
            high REAL,
            low REAL,
            close REAL,
            volume REAL,
            tick_count INTEGER,
            source TEXT,
            created_at TEXT,
            UNIQUE(bucket_time, symbol, source)
        )
        ''')

        cur.execute('''
        CREATE TABLE IF NOT EXISTS spread_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            datetime TEXT NOT NULL,
            symbol TEXT NOT NULL,
            bid REAL,
            ask REAL,
            spread REAL,
            spread_pct REAL,
            bid_qty REAL,
            ask_qty REAL,
            last_price REAL,
            source TEXT,
            created_at TEXT
        )
        ''')

        cur.execute('''
        CREATE INDEX IF NOT EXISTS idx_five_sec_bars_symbol_time
        ON five_sec_bars(symbol, bucket_time)
        ''')

        cur.execute('''
        CREATE INDEX IF NOT EXISTS idx_spread_snapshots_symbol_time
        ON spread_snapshots(symbol, datetime)
        ''')

        conn.commit()
        conn.close()


def audit_five_sec_bar(symbol: str, bucket_time: Any, open_price: Any, high_price: Any, low_price: Any, close_price: Any, volume: Any = 0, tick_count: Any = 0, source: str = 'PUSH_5S') -> None:
    """5秒OHLCVを保存する。既存行は上書きする。"""
    try:
        ensure_market_audit_db()
        with _LOCK:
            conn = _connect()
            conn.execute('''
            INSERT OR REPLACE INTO five_sec_bars (
                bucket_time, symbol, open, high, low, close,
                volume, tick_count, source, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                str(bucket_time),
                str(symbol),
                _safe_float(open_price),
                _safe_float(high_price),
                _safe_float(low_price),
                _safe_float(close_price),
                _safe_float(volume),
                _safe_int(tick_count),
                str(source or 'PUSH_5S'),
                datetime.now().isoformat(timespec='seconds'),
            ))
            conn.commit()
            conn.close()
    except Exception:
        return


def audit_spread_snapshot(symbol: str, bid: Any = None, ask: Any = None, bid_qty: Any = None, ask_qty: Any = None, last_price: Any = None, source: str = 'BOARD') -> None:
    """板/スプレッドを保存する。"""
    try:
        ensure_market_audit_db()
        bid_f = _safe_float(bid, 0.0)
        ask_f = _safe_float(ask, 0.0)
        last_f = _safe_float(last_price, 0.0)
        spread = 0.0
        spread_pct = 0.0

        if bid_f > 0 and ask_f > 0:
            spread = ask_f - bid_f
            mid = (ask_f + bid_f) / 2.0
            if mid > 0:
                spread_pct = spread / mid * 100.0

        with _LOCK:
            conn = _connect()
            conn.execute('''
            INSERT INTO spread_snapshots (
                datetime, symbol, bid, ask, spread, spread_pct,
                bid_qty, ask_qty, last_price, source, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                datetime.now().isoformat(timespec='seconds'),
                str(symbol),
                bid_f,
                ask_f,
                spread,
                spread_pct,
                _safe_float(bid_qty, 0.0),
                _safe_float(ask_qty, 0.0),
                last_f,
                str(source or 'BOARD'),
                datetime.now().isoformat(timespec='seconds'),
            ))
            conn.commit()
            conn.close()
    except Exception:
        return


def audit_spread_from_quotes(symbol: str, quotes: dict | None, source: str = 'get_latest_bid_ask') -> None:
    """get_latest_bid_ask の戻り値らしき dict からスプレッドを保存する。"""
    try:
        if not isinstance(quotes, dict):
            return
        audit_spread_snapshot(
            symbol=symbol,
            bid=quotes.get('bid_price') or quotes.get('BidPrice') or quotes.get('bid'),
            ask=quotes.get('ask_price') or quotes.get('AskPrice') or quotes.get('ask'),
            bid_qty=quotes.get('bid_qty') or quotes.get('BidQty') or quotes.get('bid_volume'),
            ask_qty=quotes.get('ask_qty') or quotes.get('AskQty') or quotes.get('ask_volume'),
            last_price=quotes.get('current_price') or quotes.get('CurrentPrice') or quotes.get('price') or quotes.get('last_price'),
            source=source,
        )
    except Exception:
        return
