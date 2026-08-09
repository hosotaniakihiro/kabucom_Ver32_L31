# ============================================================
# File   : trading/audit_logging/recorder.py
# Version: Ver03-AUDIT-RECORDER-FULL-TRADE-LINK-NATIVE
# ------------------------------------------------------------
# Native (post core/startup patch integration) version.
#
# Formerly, three core/startup/*_patch.py runtime patches
# (installed in this order by install_audit_logging.py):
#   1. position_trade_reason_schema_patch  - adds entry_id/reason/
#      technical_snapshot columns to positions.db tables
#      (entry_log/exit_log/trade_history/positions/trade_exit_stats).
#   2. audit_full_trade_link_patch        - wraps ensure_audit_db /
#      record_* to also write entry_id/reason_code/ranking metadata /
#      technical_snapshot into "extra" columns of this module's audit
#      db tables.
#   3. audit_full_trade_link_no_duplicate_patch - installed last, so
#      it is the function object that actually ends up bound as
#      recorder.record_* / recorder.ensure_audit_db at runtime. It
#      reuses patch (2)'s _insert_extra/_ensure_extra_schema/
#      build_entry_id helpers but replaces record_* entirely (instead
#      of calling the previous record_* first, like patch (2) does)
#      so that each event produces exactly one audit row instead of
#      two.
#
# This file now contains that final composed behavior natively:
# record_candidate_event/record_filter_event/record_order_event/
# record_exit_decision/record_position_state insert a single row via
# _insert_extra() (base + extra columns merged, no duplicate insert,
# no ensure_audit_db() call - matching patch (3)'s winning override).
# ensure_audit_db() itself is left as the original base-table creator
# below: that is exactly the function object that was already bound
# and invoked once, synchronously, at the top of
# install_audit_logging() before any of the three patches above had a
# chance to rebind trading.audit_logging.recorder.ensure_audit_db, so
# it is what actually created the base tables in the patched system
# too. position_trade_reason_schema_patch's positions.db schema logic
# is ported as ensure_position_trade_reason_schema() below.
# ============================================================

import datetime as dt
import logging
import os
import sqlite3
import threading
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

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


def _norm_symbol(v: Any) -> str:
    try:
        s = str(v or '').strip()
        if s.endswith('.0'):
            s = s[:-2]
        return s
    except Exception:
        return ''


def build_entry_id(*, source: Any = '', symbol: Any = '', side: Any = '', when: Any = '') -> str:
    """Deterministic entry_id linking candidate/order/exit/position rows for one trade.

    (native of core/startup/audit_full_trade_link_patch.build_entry_id)
    """
    src = str(source or 'UNKNOWN').strip().upper() or 'UNKNOWN'
    sym = _norm_symbol(symbol) or 'UNKNOWN'
    sd = str(side or '').strip().upper() or 'NA'
    w = str(when or dt.datetime.now().isoformat(timespec='seconds'))
    keep = ''.join(ch for ch in w if ch.isdigit())[:14] or dt.datetime.now().strftime('%Y%m%d%H%M%S')
    return f'{src}-{keep}-{sym}-{sd}'


def _ensure_extra_schema() -> None:
    """Add entry_id/reason/ranking/technical_snapshot columns + entry_id indexes.

    (native of core/startup/audit_full_trade_link_patch._ensure_extra_schema)
    """
    try:
        path = get_audit_db_path()
        with sqlite3.connect(path, timeout=30) as conn:
            cur = conn.cursor()
            cur.execute('PRAGMA busy_timeout=30000')
            extras = {
                'candidate_history': {
                    'entry_id': 'entry_id TEXT',
                    'reason_code': 'reason_code TEXT',
                    'ranking_type': 'ranking_type TEXT',
                    'rank_position': 'rank_position INTEGER',
                    'ranking_snapshot_time': 'ranking_snapshot_time TEXT',
                    'technical_snapshot': 'technical_snapshot TEXT',
                },
                'filter_history': {
                    'entry_id': 'entry_id TEXT',
                    'source': 'source TEXT',
                    'side': 'side TEXT',
                },
                'order_history': {
                    'entry_id': 'entry_id TEXT',
                    'reason': 'reason TEXT',
                    'entry_source': 'entry_source TEXT',
                    'entry_mode': 'entry_mode TEXT',
                    'technical_snapshot': 'technical_snapshot TEXT',
                },
                'exit_history': {
                    'entry_id': 'entry_id TEXT',
                    'pnl': 'pnl REAL',
                    'pnl_pct': 'pnl_pct REAL',
                    'holding_seconds': 'holding_seconds REAL',
                    'technical_snapshot': 'technical_snapshot TEXT',
                },
                'position_state_history': {
                    'entry_id': 'entry_id TEXT',
                    'current_price': 'current_price REAL',
                    'pnl_pct': 'pnl_pct REAL',
                    'technical_snapshot': 'technical_snapshot TEXT',
                },
            }
            for table, cols in extras.items():
                for col, ddl in cols.items():
                    _ensure_column(cur, table, col, ddl)
            for sql in (
                'CREATE INDEX IF NOT EXISTS idx_candidate_history_entry_id ON candidate_history(entry_id)',
                'CREATE INDEX IF NOT EXISTS idx_order_history_entry_id ON order_history(entry_id)',
                'CREATE INDEX IF NOT EXISTS idx_exit_history_entry_id ON exit_history(entry_id)',
                'CREATE INDEX IF NOT EXISTS idx_position_state_history_entry_id ON position_state_history(entry_id)',
            ):
                try:
                    cur.execute(sql)
                except Exception:
                    pass
            conn.commit()
    except Exception:
        logger.debug('[AUDIT LOGGER] extra schema failed', exc_info=True)


def _insert_extra(table: str, values: dict) -> None:
    """Insert one row using only the columns that already exist on `table`.

    (native of core/startup/audit_full_trade_link_patch._insert_extra)
    """
    try:
        _ensure_extra_schema()
        path = get_audit_db_path()
        with sqlite3.connect(path, timeout=30) as conn:
            cols_all = {str(r[1]) for r in conn.execute(f'PRAGMA table_info("{table}")').fetchall()}
            vals = {k: v for k, v in values.items() if k in cols_all}
            if not vals:
                return
            cols = list(vals.keys())
            ph = ','.join(['?'] * len(cols))
            sql = f'INSERT INTO "{table}" ({",".join(cols)}) VALUES ({ph})'
            conn.execute(sql, tuple(vals[c] for c in cols))
            conn.commit()
    except Exception:
        logger.debug('[AUDIT LOGGER] insert extra failed table=%s', table, exc_info=True)


def _position_db_path() -> str:
    try:
        from config.paths import get_path
        return os.path.join(str(get_path('runtime_positions')), 'positions.db')
    except Exception:
        return r'\\192.168.0.22\AutoStockBuyAndSell\runtime_positions\positions.db'


def _ensure_position_col(cur, table: str, col: str, ddl: str) -> bool:
    try:
        cols = {str(r[1]) for r in cur.execute(f'PRAGMA table_info("{table}")').fetchall()}
        if col in cols:
            return False
        cur.execute(f'ALTER TABLE "{table}" ADD COLUMN {ddl}')
        return True
    except Exception as e:
        msg = str(e).lower()
        if 'duplicate column' in msg or 'already exists' in msg:
            return False
        logger.debug('[POSITION REASON SCHEMA] add column failed table=%s col=%s', table, col, exc_info=True)
        return False


def ensure_position_trade_reason_schema() -> dict:
    """Add entry_id/reason/technical_snapshot/link columns to positions.db tables.

    (native of core/startup/position_trade_reason_schema_patch.ensure_schema)
    SQLAlchemy create_all() does not alter existing SQLite tables, so this keeps
    old positions.db files compatible while allowing code to store entry_id/
    trade_id links, entry_source/entry_mode, entry_reason/exit_reason and
    technical_snapshot JSON.
    """
    path = _position_db_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    added: dict = {}
    with sqlite3.connect(path, timeout=30) as conn:
        cur = conn.cursor()
        cur.execute('PRAGMA busy_timeout=30000')
        try:
            cur.execute('PRAGMA journal_mode=WAL')
        except Exception:
            pass
        schema = {
            'entry_log': {
                'entry_id': 'entry_id TEXT',
                'entry_reason': 'entry_reason TEXT',
                'reason_code': 'reason_code TEXT',
                'technical_snapshot': 'technical_snapshot TEXT',
                'order_id': 'order_id TEXT',
            },
            'exit_log': {
                'entry_id': 'entry_id TEXT',
                'exit_source': 'exit_source TEXT',
                'reason_code': 'reason_code TEXT',
                'technical_snapshot': 'technical_snapshot TEXT',
            },
            'trade_history': {
                'trade_id': 'trade_id TEXT',
                'entry_id': 'entry_id TEXT',
                'entry_source': 'entry_source TEXT',
                'entry_mode': 'entry_mode TEXT',
                'entry_reason': 'entry_reason TEXT',
                'exit_reason': 'exit_reason TEXT',
                'reason_code': 'reason_code TEXT',
                'technical_snapshot': 'technical_snapshot TEXT',
                'audit_snapshot_ref': 'audit_snapshot_ref TEXT',
            },
            'positions': {
                'entry_id': 'entry_id TEXT',
                'entry_source': 'entry_source TEXT',
                'entry_mode': 'entry_mode TEXT',
                'entry_reason': 'entry_reason TEXT',
                'reason_code': 'reason_code TEXT',
                'technical_snapshot': 'technical_snapshot TEXT',
            },
            'trade_exit_stats': {
                'entry_id': 'entry_id TEXT',
                'technical_snapshot': 'technical_snapshot TEXT',
                'exit_snapshot': 'exit_snapshot TEXT',
            },
        }
        for table, cols in schema.items():
            added[table] = []
            try:
                exists = cur.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
                ).fetchone()
                if not exists:
                    continue
            except Exception:
                continue
            for col, ddl in cols.items():
                if _ensure_position_col(cur, table, col, ddl):
                    added[table].append(col)
        for sql in (
            'CREATE INDEX IF NOT EXISTS idx_entry_log_entry_id ON entry_log(entry_id)',
            'CREATE INDEX IF NOT EXISTS idx_exit_log_entry_id ON exit_log(entry_id)',
            'CREATE INDEX IF NOT EXISTS idx_trade_history_entry_id ON trade_history(entry_id)',
            'CREATE INDEX IF NOT EXISTS idx_trade_history_trade_id ON trade_history(trade_id)',
            'CREATE INDEX IF NOT EXISTS idx_positions_entry_id ON positions(entry_id)',
        ):
            try:
                cur.execute(sql)
            except Exception:
                pass
        conn.commit()
    return added


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


# ------------------------------------------------------------------
# record_candidate_event / record_filter_event / record_order_event /
# record_exit_decision / record_position_state
#
# Native of core/startup/audit_full_trade_link_no_duplicate_patch.py's
# record_* functions (the ones that end up bound at runtime once all
# three patches in this cluster are installed). Each event now writes
# exactly one row via _insert_extra(), covering both the original base
# columns and the extra entry_id/reason/ranking/technical_snapshot
# columns added by _ensure_extra_schema(). Unlike the pre-integration
# native functions, these do not call ensure_audit_db() themselves -
# _insert_extra() ensures the extra columns exist on every call, and
# the base tables are expected to already exist (created by the
# ensure_audit_db() call at the top of install_audit_logging()).
# ------------------------------------------------------------------

def record_candidate_event(**kwargs):
    source = kwargs.get('source') or ''
    symbol = kwargs.get('symbol') or ''
    side = kwargs.get('side') or ''
    when = kwargs.get('datetime') or dt.datetime.now().isoformat(timespec='seconds')
    entry_id = kwargs.get('entry_id') or build_entry_id(source=source, symbol=symbol, side=side, when=when)
    _insert_extra('candidate_history', {
        'datetime': when,
        'symbol': _norm_symbol(symbol),
        'side': str(side or '').upper(),
        'source': source,
        'interval_min': kwargs.get('interval_min'),
        'score_buy': kwargs.get('score_buy'),
        'score_sell': kwargs.get('score_sell'),
        'score_total': kwargs.get('score_total'),
        'final_score': kwargs.get('final_score'),
        'ai_result': kwargs.get('ai_result'),
        'reason': kwargs.get('reason'),
        'technical_snapshot': kwargs.get('technical_snapshot'),
        'entry_id': entry_id,
        'reason_code': kwargs.get('reason_code'),
        'ranking_type': kwargs.get('ranking_type'),
        'rank_position': kwargs.get('rank_position'),
        'ranking_snapshot_time': kwargs.get('ranking_snapshot_time'),
        'created_at': dt.datetime.now().isoformat(),
    })


def record_filter_event(**kwargs):
    _insert_extra('filter_history', {
        'datetime': kwargs.get('datetime') or dt.datetime.now().isoformat(timespec='seconds'),
        'symbol': _norm_symbol(kwargs.get('symbol')),
        'filter_name': kwargs.get('filter_name'),
        'passed': 1 if kwargs.get('passed') else 0,
        'detail': kwargs.get('detail'),
        'entry_id': kwargs.get('entry_id'),
        'source': kwargs.get('source'),
        'side': kwargs.get('side'),
        'created_at': dt.datetime.now().isoformat(),
    })


def record_order_event(**kwargs):
    reason = kwargs.get('reason') or kwargs.get('cancel_reason')
    _insert_extra('order_history', {
        'datetime': kwargs.get('datetime') or dt.datetime.now().isoformat(timespec='seconds'),
        'symbol': _norm_symbol(kwargs.get('symbol')),
        'side': str(kwargs.get('side') or '').upper(),
        'qty': kwargs.get('qty'),
        'order_type': kwargs.get('order_type'),
        'order_id': kwargs.get('order_id'),
        'status': kwargs.get('status'),
        'price': kwargs.get('price'),
        'filled_price': kwargs.get('filled_price'),
        'cancel_reason': kwargs.get('cancel_reason') or reason,
        'entry_id': kwargs.get('entry_id'),
        'reason': reason,
        'entry_source': kwargs.get('entry_source') or kwargs.get('source'),
        'entry_mode': kwargs.get('entry_mode'),
        'technical_snapshot': kwargs.get('technical_snapshot'),
        'created_at': dt.datetime.now().isoformat(),
    })


def record_exit_decision(**kwargs):
    _insert_extra('exit_history', {
        'datetime': kwargs.get('datetime') or dt.datetime.now().isoformat(timespec='seconds'),
        'symbol': _norm_symbol(kwargs.get('symbol')),
        'side': str(kwargs.get('side') or '').upper(),
        'entry_price': kwargs.get('entry_price'),
        'current_price': kwargs.get('current_price'),
        'highest_since_entry': kwargs.get('highest_since_entry'),
        'lowest_since_entry': kwargs.get('lowest_since_entry'),
        'exit_reason': kwargs.get('exit_reason'),
        'triggered': 1 if kwargs.get('triggered') else 0,
        'entry_id': kwargs.get('entry_id'),
        'pnl': kwargs.get('pnl'),
        'pnl_pct': kwargs.get('pnl_pct'),
        'holding_seconds': kwargs.get('holding_seconds'),
        'technical_snapshot': kwargs.get('technical_snapshot'),
        'created_at': dt.datetime.now().isoformat(),
    })


def record_position_state(**kwargs):
    _insert_extra('position_state_history', {
        'datetime': kwargs.get('datetime') or dt.datetime.now().isoformat(timespec='seconds'),
        'symbol': _norm_symbol(kwargs.get('symbol')),
        'side': str(kwargs.get('side') or '').upper(),
        'qty': kwargs.get('qty'),
        'entry_price': kwargs.get('entry_price'),
        'highest_since_entry': kwargs.get('highest_since_entry'),
        'lowest_since_entry': kwargs.get('lowest_since_entry'),
        'holding_seconds': kwargs.get('holding_seconds'),
        'entry_id': kwargs.get('entry_id'),
        'current_price': kwargs.get('current_price'),
        'pnl_pct': kwargs.get('pnl_pct'),
        'technical_snapshot': kwargs.get('technical_snapshot'),
        'created_at': dt.datetime.now().isoformat(),
    })
