# ============================================================
# File   : trading/news_learning/news_price_reaction_store.py
# Version: Ver01-NEWS-PRICE-REACTION-STORE
# ------------------------------------------------------------
# ニュース後の翌営業日値動きを保存する。
#
# 目的:
#   - ニュース後にどれくらいGU/GDするか
#   - 寄り天率
#   - 5分後/30分後/引けの強さ
#   - 材料別の期待値
#
# summary DB / yahoo summary / replay と組み合わせて学習する。
# ============================================================

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

BASE_DIR = r'\\192.168.0.22\AutoStockBuyAndSell\raw_data\news_learning'


def get_news_reaction_db_path() -> str:
    os.makedirs(BASE_DIR, exist_ok=True)
    return os.path.join(BASE_DIR, 'news_price_reaction.db')


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA synchronous=NORMAL')
    return conn


def ensure_news_reaction_db() -> str:
    path = get_news_reaction_db_path()
    with _connect(path) as conn:
        conn.execute(
            '''
            CREATE TABLE IF NOT EXISTS news_price_reactions (
                event_id TEXT PRIMARY KEY,
                symbol TEXT,
                news_type TEXT,
                sentiment REAL,
                importance REAL,
                published_at TEXT,
                reaction_trade_date TEXT,
                prev_close REAL,
                next_open REAL,
                next_high REAL,
                next_low REAL,
                next_close REAL,
                open_return_pct REAL,
                high_return_pct REAL,
                low_return_pct REAL,
                close_return_pct REAL,
                move_5m_pct REAL,
                move_30m_pct REAL,
                max_gain_pct REAL,
                max_drawdown_pct REAL,
                volume_ratio REAL,
                gap_type TEXT,
                created_at TEXT
            )
            '''
        )
        conn.execute('CREATE INDEX IF NOT EXISTS idx_news_reaction_symbol ON news_price_reactions(symbol, reaction_trade_date)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_news_reaction_type ON news_price_reactions(news_type)')
        conn.commit()
    return path


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == '':
            return default
        return float(v)
    except Exception:
        return default


def _pct(a: float, b: float) -> float:
    if not a:
        return 0.0
    return ((b - a) / a) * 100.0


def save_news_price_reaction(
    *,
    event_id: str,
    symbol: str,
    news_type: str = '',
    sentiment: Any = 0.0,
    importance: Any = 0.0,
    published_at: Any = '',
    reaction_trade_date: str = '',
    prev_close: Any = 0.0,
    next_open: Any = 0.0,
    next_high: Any = 0.0,
    next_low: Any = 0.0,
    next_close: Any = 0.0,
    move_5m_pct: Any = 0.0,
    move_30m_pct: Any = 0.0,
    volume_ratio: Any = 0.0,
) -> str:
    path = ensure_news_reaction_db()

    prev_close = _safe_float(prev_close)
    next_open = _safe_float(next_open)
    next_high = _safe_float(next_high)
    next_low = _safe_float(next_low)
    next_close = _safe_float(next_close)

    open_ret = _pct(prev_close, next_open)
    high_ret = _pct(prev_close, next_high)
    low_ret = _pct(prev_close, next_low)
    close_ret = _pct(prev_close, next_close)

    if open_ret >= 3:
        gap_type = 'GU_STRONG'
    elif open_ret > 0:
        gap_type = 'GU'
    elif open_ret <= -3:
        gap_type = 'GD_STRONG'
    elif open_ret < 0:
        gap_type = 'GD'
    else:
        gap_type = 'FLAT'

    now = datetime.now().isoformat(timespec='seconds')

    with _connect(path) as conn:
        conn.execute(
            '''
            INSERT OR REPLACE INTO news_price_reactions (
                event_id, symbol, news_type, sentiment, importance,
                published_at, reaction_trade_date,
                prev_close, next_open, next_high, next_low, next_close,
                open_return_pct, high_return_pct, low_return_pct, close_return_pct,
                move_5m_pct, move_30m_pct,
                max_gain_pct, max_drawdown_pct,
                volume_ratio, gap_type, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            (
                str(event_id),
                str(symbol),
                str(news_type),
                _safe_float(sentiment),
                _safe_float(importance),
                str(published_at),
                str(reaction_trade_date),
                prev_close,
                next_open,
                next_high,
                next_low,
                next_close,
                open_ret,
                high_ret,
                low_ret,
                close_ret,
                _safe_float(move_5m_pct),
                _safe_float(move_30m_pct),
                high_ret,
                low_ret,
                _safe_float(volume_ratio),
                gap_type,
                now,
            ),
        )
        conn.commit()

    logger.info(
        '[NEWS REACTION] saved event_id=%s symbol=%s type=%s open=%.2f%% high=%.2f%% close=%.2f%% gap=%s',
        event_id,
        symbol,
        news_type,
        open_ret,
        high_ret,
        close_ret,
        gap_type,
    )
    return event_id
