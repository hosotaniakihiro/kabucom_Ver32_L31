# ============================================================
# File   : trading/audit_logging/recorder.py
# Version: Ver02-AUDIT-RECORDER-TECHNICAL-SNAPSHOT
# ============================================================

import os
import sqlite3
import threading
from datetime import datetime

_LOCK = threading.Lock()

BASE_DIR = r'\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\audit'


def _today():
    return datetime.now().strftime('%Y%m%d')


def get_audit_db_path():
    os.makedirs(BASE_DIR, exist_ok=True)
    return os.path.join(BASE_DIR, f'audit_{_today()}.db')


def _connect():
    conn = sqlite3.connect(get_audit_db_path(), timeout=30)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA synchronous=NORMAL')
    return conn


def _ensure_column(cur, table: str, column: str, ddl: str) -> None:
    try:
        cols = {str(r[1]) for r in cur.execute(f'PRAGMA table_info("{table}")').fetchall()}
        if column not in cols:
            cur.execute(f'ALTER TABLE "{table}" ADD COLUMN {ddl}')
    except Exception:
        # Schema migration failure must not stop trading. Insert failures will be
        # handled by the caller and printed by _insert.
        pass


def ensure_audit_db():
    with _LOCK:
        conn = _connect()
        cur = conn.cursor()

        cur.execute('''
        CREATE TABLE IF NOT EXISTS candidate_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            datetime TEXT,
            symbol TEXT,
            side TEXT,
            source TEXT,
            interval_min INTEGER,
            score_buy REAL,
            score_sell REAL,
            score_total REAL,
            final_score REAL,
            ai_result TEXT,
            reason TEXT,
            technical_snapshot TEXT,
            created_at TEXT
        )
        ''')
        _ensure_column(cur, 'candidate_history', 'technical_snapshot', 'technical_snapshot TEXT')

        cur.execute('''
        CREATE TABLE IF NOT EXISTS filter_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            datetime TEXT,
            symbol TEXT,
            filter_name TEXT,
            passed INTEGER,
            detail TEXT,
            created_at TEXT
        )
        ''')

        cur.execute('''
        CREATE TABLE IF NOT EXISTS order_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            datetime TEXT,
            symbol TEXT,
            side TEXT,
            qty INTEGER,
            order_type TEXT,
            order_id TEXT,
            status TEXT,
            price REAL,
            filled_price REAL,
            cancel_reason TEXT,
            created_at TEXT
        )
        ''')

        cur.execute('''
        CREATE TABLE IF NOT EXISTS exit_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            datetime TEXT,
            symbol TEXT,
            side TEXT,
            entry_price REAL,
            current_price REAL,
            highest_since_entry REAL,
            lowest_since_entry REAL,
            exit_reason TEXT,
            triggered INTEGER,
            created_at TEXT
        )
        ''')

        cur.execute('''
        CREATE TABLE IF NOT EXISTS position_state_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            datetime TEXT,
            symbol TEXT,
            side TEXT,
            qty INTEGER,
            entry_price REAL,
            highest_since_entry REAL,
            lowest_since_entry REAL,
            holding_seconds REAL,
            created_at TEXT
        )
        ''')

        conn.commit()
        conn.close()


def _insert(sql, values):
    try:
        with _LOCK:
            conn = _connect()
            conn.execute(sql, values)
            conn.commit()
            conn.close()
    except Exception as e:
        print(f'[AUDIT LOGGER] insert failed: {e}')


def record_candidate_event(**kwargs):
    ensure_audit_db()
    sql = '''
    INSERT INTO candidate_history (
        datetime, symbol, side, source, interval_min,
        score_buy, score_sell, score_total, final_score,
        ai_result, reason, technical_snapshot, created_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    '''

    values = (
        kwargs.get('datetime'),
        kwargs.get('symbol'),
        kwargs.get('side'),
        kwargs.get('source'),
        kwargs.get('interval_min'),
        kwargs.get('score_buy'),
        kwargs.get('score_sell'),
        kwargs.get('score_total'),
        kwargs.get('final_score'),
        kwargs.get('ai_result'),
        kwargs.get('reason'),
        kwargs.get('technical_snapshot'),
        datetime.now().isoformat(),
    )

    _insert(sql, values)


def record_filter_event(**kwargs):
    ensure_audit_db()
    sql = '''
    INSERT INTO filter_history (
        datetime, symbol, filter_name,
        passed, detail, created_at
    ) VALUES (?, ?, ?, ?, ?, ?)
    '''

    values = (
        kwargs.get('datetime'),
        kwargs.get('symbol'),
        kwargs.get('filter_name'),
        1 if kwargs.get('passed') else 0,
        kwargs.get('detail'),
        datetime.now().isoformat(),
    )

    _insert(sql, values)


def record_order_event(**kwargs):
    ensure_audit_db()
    sql = '''
    INSERT INTO order_history (
        datetime, symbol, side, qty,
        order_type, order_id, status,
        price, filled_price, cancel_reason,
        created_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    '''

    values = (
        kwargs.get('datetime'),
        kwargs.get('symbol'),
        kwargs.get('side'),
        kwargs.get('qty'),
        kwargs.get('order_type'),
        kwargs.get('order_id'),
        kwargs.get('status'),
        kwargs.get('price'),
        kwargs.get('filled_price'),
        kwargs.get('cancel_reason'),
        datetime.now().isoformat(),
    )

    _insert(sql, values)


def record_exit_decision(**kwargs):
    ensure_audit_db()
    sql = '''
    INSERT INTO exit_history (
        datetime, symbol, side,
        entry_price, current_price,
        highest_since_entry, lowest_since_entry,
        exit_reason, triggered,
        created_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    '''

    values = (
        kwargs.get('datetime'),
        kwargs.get('symbol'),
        kwargs.get('side'),
        kwargs.get('entry_price'),
        kwargs.get('current_price'),
        kwargs.get('highest_since_entry'),
        kwargs.get('lowest_since_entry'),
        kwargs.get('exit_reason'),
        1 if kwargs.get('triggered') else 0,
        datetime.now().isoformat(),
    )

    _insert(sql, values)


def record_position_state(**kwargs):
    ensure_audit_db()
    sql = '''
    INSERT INTO position_state_history (
        datetime, symbol, side,
        qty, entry_price,
        highest_since_entry, lowest_since_entry,
        holding_seconds,
        created_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    '''

    values = (
        kwargs.get('datetime'),
        kwargs.get('symbol'),
        kwargs.get('side'),
        kwargs.get('qty'),
        kwargs.get('entry_price'),
        kwargs.get('highest_since_entry'),
        kwargs.get('lowest_since_entry'),
        kwargs.get('holding_seconds'),
        datetime.now().isoformat(),
    )

    _insert(sql, values)
