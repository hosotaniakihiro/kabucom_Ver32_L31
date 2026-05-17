# ============================================================
# File   : trading/news_learning/news_event_store.py
# Version: Ver01-NEWS-EVENT-STORE
# ------------------------------------------------------------
# 株価ニュース/IR/材料イベントをDBへ保存する。
#
# 目的:
#   - ニュースの種類と翌日の値動きの関係を学習する
#   - ニュース単体ではなく、ニュース×銘柄特性×地合いで分析する
#
# このモジュールはニュース取得元に依存しない。
# 外部から headline / symbol / published_at / news_type を渡して保存する。
# ============================================================

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

BASE_DIR = r'\\192.168.0.22\AutoStockBuyAndSell\raw_data\news_learning'


def _today() -> str:
    return datetime.now().strftime('%Y%m%d')


def get_news_event_db_path() -> str:
    os.makedirs(BASE_DIR, exist_ok=True)
    return os.path.join(BASE_DIR, 'news_event_history.db')


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA synchronous=NORMAL')
    return conn


def _normalize_symbol(symbol: Any) -> str:
    s = str(symbol or '').strip().upper()
    if s.endswith('.T'):
        s = s[:-2]
    if s.endswith('.0') and s[:-2].isdigit():
        s = s[:-2]
    return s


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


def ensure_news_event_db() -> str:
    path = get_news_event_db_path()
    with _connect(path) as conn:
        conn.execute(
            '''
            CREATE TABLE IF NOT EXISTS news_events (
                event_id TEXT PRIMARY KEY,
                symbol TEXT,
                symbol_name TEXT,
                headline TEXT,
                body TEXT,
                source TEXT,
                url TEXT,
                published_at TEXT,
                detected_at TEXT,
                news_type TEXT,
                sentiment REAL,
                importance REAL,
                is_ir INTEGER,
                is_earnings INTEGER,
                is_revision INTEGER,
                is_tob INTEGER,
                is_partnership INTEGER,
                is_financing INTEGER,
                is_regulation INTEGER,
                payload_json TEXT,
                created_at TEXT
            )
            '''
        )
        conn.execute('CREATE INDEX IF NOT EXISTS idx_news_events_symbol_time ON news_events(symbol, published_at)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_news_events_type ON news_events(news_type)')
        conn.commit()
    return path


def make_event_id(symbol: str, headline: str, published_at: Any = '') -> str:
    base = f'{_normalize_symbol(symbol)}|{str(published_at)}|{str(headline).strip()}'
    return hashlib.sha1(base.encode('utf-8', errors='ignore')).hexdigest()


def classify_news_type(headline: str, body: str = '') -> dict:
    """簡易キーワード分類。後でAI分類に差し替え可能。"""
    text = f'{headline} {body}'.lower()
    flags = {
        'is_ir': int(any(k in text for k in ['ir', '適時開示', '開示', 'tdnet'])),
        'is_earnings': int(any(k in text for k in ['決算', '増益', '減益', '黒字', '赤字', '通期', '四半期'])),
        'is_revision': int(any(k in text for k in ['上方修正', '下方修正', '業績予想', '配当予想', '増配', '減配'])),
        'is_tob': int(any(k in text for k in ['tob', '公開買付', '買付け', 'mbo'])),
        'is_partnership': int(any(k in text for k in ['提携', '協業', '共同開発', '業務提携', '資本業務提携'])),
        'is_financing': int(any(k in text for k in ['増資', '第三者割当', '新株予約権', 'msワラント', '希薄化'])),
        'is_regulation': int(any(k in text for k in ['承認', '認可', '許可', '規制', '行政処分'])),
    }

    if flags['is_tob']:
        news_type = 'TOB'
        importance = 1.0
        sentiment = 1.0
    elif flags['is_revision']:
        news_type = 'REVISION'
        importance = 0.9
        sentiment = 0.7 if '上方' in headline or '増配' in headline else -0.7
    elif flags['is_earnings']:
        news_type = 'EARNINGS'
        importance = 0.8
        sentiment = 0.3
    elif flags['is_financing']:
        news_type = 'FINANCING'
        importance = 0.7
        sentiment = -0.6
    elif flags['is_partnership']:
        news_type = 'PARTNERSHIP'
        importance = 0.6
        sentiment = 0.4
    elif flags['is_regulation']:
        news_type = 'REGULATION'
        importance = 0.6
        sentiment = 0.3
    elif flags['is_ir']:
        news_type = 'IR'
        importance = 0.5
        sentiment = 0.0
    else:
        news_type = 'NEWS'
        importance = 0.3
        sentiment = 0.0

    return {
        'news_type': news_type,
        'importance': importance,
        'sentiment': sentiment,
        **flags,
    }


def save_news_event(
    *,
    symbol: str,
    headline: str,
    symbol_name: str = '',
    body: str = '',
    source: str = '',
    url: str = '',
    published_at: Any = None,
    detected_at: Any = None,
    news_type: str | None = None,
    sentiment: Any = None,
    importance: Any = None,
    payload: dict | None = None,
) -> str:
    path = ensure_news_event_db()
    symbol = _normalize_symbol(symbol)
    now = datetime.now().isoformat(timespec='seconds')
    pub_s = published_at.isoformat(timespec='seconds') if isinstance(published_at, datetime) else str(published_at or now)
    det_s = detected_at.isoformat(timespec='seconds') if isinstance(detected_at, datetime) else str(detected_at or now)

    cls = classify_news_type(headline, body)
    if news_type:
        cls['news_type'] = str(news_type)
    if sentiment is not None:
        cls['sentiment'] = _safe_float(sentiment, cls.get('sentiment', 0.0))
    if importance is not None:
        cls['importance'] = _safe_float(importance, cls.get('importance', 0.0))

    event_id = make_event_id(symbol, headline, pub_s)
    with _connect(path) as conn:
        conn.execute(
            '''
            INSERT OR REPLACE INTO news_events (
                event_id, symbol, symbol_name, headline, body, source, url,
                published_at, detected_at, news_type, sentiment, importance,
                is_ir, is_earnings, is_revision, is_tob, is_partnership,
                is_financing, is_regulation, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            (
                event_id,
                symbol,
                str(symbol_name or ''),
                str(headline or ''),
                str(body or ''),
                str(source or ''),
                str(url or ''),
                pub_s,
                det_s,
                str(cls.get('news_type') or ''),
                _safe_float(cls.get('sentiment'), 0.0),
                _safe_float(cls.get('importance'), 0.0),
                _safe_int(cls.get('is_ir'), 0),
                _safe_int(cls.get('is_earnings'), 0),
                _safe_int(cls.get('is_revision'), 0),
                _safe_int(cls.get('is_tob'), 0),
                _safe_int(cls.get('is_partnership'), 0),
                _safe_int(cls.get('is_financing'), 0),
                _safe_int(cls.get('is_regulation'), 0),
                json.dumps(payload or {}, ensure_ascii=False, default=str),
                now,
            ),
        )
        conn.commit()

    logger.info('[NEWS EVENT] saved event_id=%s symbol=%s type=%s headline=%s', event_id, symbol, cls.get('news_type'), headline)
    return event_id


def load_news_events(symbol: str | None = None, since: str | None = None, until: str | None = None) -> list[dict]:
    path = ensure_news_event_db()
    where = []
    params: list[Any] = []
    if symbol:
        where.append('symbol=?')
        params.append(_normalize_symbol(symbol))
    if since:
        where.append('published_at>=?')
        params.append(str(since))
    if until:
        where.append('published_at<=?')
        params.append(str(until))

    sql = 'SELECT * FROM news_events'
    if where:
        sql += ' WHERE ' + ' AND '.join(where)
    sql += ' ORDER BY published_at DESC'

    with _connect(path) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]
