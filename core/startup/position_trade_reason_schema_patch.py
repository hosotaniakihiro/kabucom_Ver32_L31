# -*- coding: utf-8 -*-
"""Add reason/snapshot/link columns to positions.db tables at startup.

SQLAlchemy create_all() does not alter existing SQLite tables, so this patch
keeps old positions.db files compatible while allowing future code to store:
- entry_id / trade_id links
- entry_source / entry_mode
- entry_reason / exit_reason
- technical_snapshot JSON
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)
VERSION = "V1-POSITION-TRADE-REASON-SCHEMA"
_INSTALLED = False


def _position_db_path() -> Path:
    try:
        from config.paths import get_path
        return Path(get_path("runtime_positions")) / "positions.db"
    except Exception:
        return Path(r"\\192.168.0.22\AutoStockBuyAndSell\runtime_positions\positions.db")


def _ensure_col(cur, table: str, col: str, ddl: str) -> bool:
    try:
        cols = {str(r[1]) for r in cur.execute(f'PRAGMA table_info("{table}")').fetchall()}
        if col in cols:
            return False
        cur.execute(f'ALTER TABLE "{table}" ADD COLUMN {ddl}')
        return True
    except Exception as e:
        msg = str(e).lower()
        if "duplicate column" in msg or "already exists" in msg:
            return False
        logger.debug("[POSITION REASON SCHEMA] add column failed table=%s col=%s", table, col, exc_info=True)
        return False


def _ensure_indexes(cur) -> None:
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


def ensure_schema() -> dict[str, list[str]]:
    path = _position_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    added: dict[str, list[str]] = {}
    with sqlite3.connect(str(path), timeout=30) as conn:
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
                exists = cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
                if not exists:
                    continue
            except Exception:
                continue
            for col, ddl in cols.items():
                if _ensure_col(cur, table, col, ddl):
                    added[table].append(col)
        _ensure_indexes(cur)
        conn.commit()
    return added


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    try:
        added = ensure_schema()
        _INSTALLED = True
        logger.warning("[POSITION REASON SCHEMA] installed version=%s added=%s", VERSION, added)
        return True
    except Exception:
        logger.exception("[POSITION REASON SCHEMA] install failed")
        return False


__all__ = ["VERSION", "install", "ensure_schema"]
