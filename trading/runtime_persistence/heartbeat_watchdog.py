# ============================================================
# File   : trading/runtime_persistence/heartbeat_watchdog.py
# Version: Ver01-HEARTBEAT-WATCHDOG
# ------------------------------------------------------------
# 日中稼働中の生存証跡をDB/JSONへ保存する。
# main.py再起動・scheduler停止・push停止・exit_loop停止の原因追跡用。
#
# このモジュールは注文を出さない。証跡保存のみ。
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
STALE_SECONDS_DEFAULT = 90


def _today() -> str:
    return datetime.now().strftime('%Y%m%d')


def get_heartbeat_db_path(trade_date: str | None = None) -> str:
    td = trade_date or _today()
    os.makedirs(BASE_DIR, exist_ok=True)
    return os.path.join(BASE_DIR, f'heartbeat_{td}.db')


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA synchronous=NORMAL')
    return conn


def ensure_heartbeat_db(trade_date: str | None = None) -> str:
    path = get_heartbeat_db_path(trade_date)
    with _connect(path) as conn:
        conn.execute(
            '''
            CREATE TABLE IF NOT EXISTS heartbeat_runtime (
                component TEXT PRIMARY KEY,
                status TEXT,
                last_seen TEXT,
                pid INTEGER,
                detail_json TEXT,
                updated_at TEXT
            )
            '''
        )
        conn.execute(
            '''
            CREATE TABLE IF NOT EXISTS heartbeat_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                component TEXT,
                event TEXT,
                status TEXT,
                detail_json TEXT,
                created_at TEXT
            )
            '''
        )
        conn.commit()
    return path


def heartbeat(component: str, status: str = 'OK', detail: dict | None = None, trade_date: str | None = None) -> None:
    """各ループから定期的に呼ぶ。例: heartbeat('exit_loop_5s')"""
    try:
        path = ensure_heartbeat_db(trade_date)
        now = datetime.now().isoformat(timespec='seconds')
        pid = os.getpid()
        detail_json = json.dumps(detail or {}, ensure_ascii=False, default=str)
        with _connect(path) as conn:
            conn.execute(
                '''
                INSERT OR REPLACE INTO heartbeat_runtime (
                    component, status, last_seen, pid, detail_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ''',
                (str(component), str(status), now, pid, detail_json, now),
            )
            conn.execute(
                '''
                INSERT INTO heartbeat_events (
                    component, event, status, detail_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                ''',
                (str(component), 'HEARTBEAT', str(status), detail_json, now),
            )
            conn.commit()
    except Exception:
        logger.debug('[HEARTBEAT] save failed component=%s', component, exc_info=True)


def mark_component_start(component: str, detail: dict | None = None, trade_date: str | None = None) -> None:
    heartbeat(component, status='START', detail=detail, trade_date=trade_date)


def mark_component_stop(component: str, detail: dict | None = None, trade_date: str | None = None) -> None:
    heartbeat(component, status='STOP', detail=detail, trade_date=trade_date)


def load_heartbeat_status(trade_date: str | None = None) -> list[dict]:
    path = ensure_heartbeat_db(trade_date)
    with _connect(path) as conn:
        cur = conn.execute(
            '''
            SELECT component, status, last_seen, pid, detail_json, updated_at
            FROM heartbeat_runtime
            ORDER BY component
            '''
        )
        cols = [x[0] for x in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def detect_stale_components(stale_seconds: int = STALE_SECONDS_DEFAULT, trade_date: str | None = None) -> dict:
    rows = load_heartbeat_status(trade_date)
    now = datetime.now()
    stale = []
    ok = []

    for r in rows:
        last_seen_s = r.get('last_seen')
        try:
            last_seen = datetime.fromisoformat(str(last_seen_s))
            age = (now - last_seen).total_seconds()
        except Exception:
            age = 999999

        item = dict(r)
        item['age_seconds'] = age
        if age > stale_seconds or str(r.get('status')).upper() in {'STOP', 'ERROR', 'NG'}:
            stale.append(item)
        else:
            ok.append(item)

    result = {
        'trade_date': trade_date or _today(),
        'stale_seconds': stale_seconds,
        'ok_count': len(ok),
        'stale_count': len(stale),
        'ok': ok,
        'stale': stale,
        'checked_at': datetime.now().isoformat(timespec='seconds'),
    }
    logger.warning('[HEARTBEAT WATCHDOG] %s', result)
    return result


def export_heartbeat_status_json(trade_date: str | None = None, stale_seconds: int = STALE_SECONDS_DEFAULT) -> str:
    os.makedirs(BASE_DIR, exist_ok=True)
    td = trade_date or _today()
    path = os.path.join(BASE_DIR, f'heartbeat_status_{td}.json')
    result = detect_stale_components(stale_seconds=stale_seconds, trade_date=td)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2, default=str)
    return path
