# ============================================================
# File   : trading/entry/symbol_performance_guard.py
# Version: Ver01-SYMBOL-PERFORMANCE-GUARD
# ------------------------------------------------------------
# 銘柄別勝率・損益統計を使ってENTRY前にフィルタする。
#
# 目的:
#   - 過去に勝率が低い銘柄をAI_OK後でも止める
#   - ブラックリスト銘柄を新規ENTRY禁止にする
#   - サンプル不足銘柄は強制禁止せず、必要に応じて低優先化できる
#
# 入力DB:
#   \\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\symbol_stats\symbol_performance_latest.db
# ============================================================

from __future__ import annotations

import logging
import os
import sqlite3
from functools import lru_cache
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = r'\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\symbol_stats\symbol_performance_latest.db'


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return bool(default)
    s = str(v).strip().lower()
    if s in {'1', 'true', 'yes', 'y', 'on', 'ok', 'enable', 'enabled'}:
        return True
    if s in {'0', 'false', 'no', 'n', 'off', 'ng', 'disable', 'disabled', ''}:
        return False
    return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.environ.get(name)
        if v is None or str(v).strip() == '':
            return float(default)
        return float(str(v).strip())
    except Exception:
        return float(default)


def _env_int(name: str, default: int) -> int:
    try:
        v = os.environ.get(name)
        if v is None or str(v).strip() == '':
            return int(default)
        return int(float(str(v).strip()))
    except Exception:
        return int(default)


ENABLE_SYMBOL_PERFORMANCE_GUARD = _env_bool('ENABLE_SYMBOL_PERFORMANCE_GUARD', True)
SYMBOL_PERFORMANCE_DB_PATH = os.environ.get('SYMBOL_PERFORMANCE_DB_PATH', DEFAULT_DB_PATH)
SYMBOL_PERFORMANCE_STRICT_BLACKLIST = _env_bool('SYMBOL_PERFORMANCE_STRICT_BLACKLIST', True)
SYMBOL_PERFORMANCE_BLOCK_LOW_WIN_RATE = _env_bool('SYMBOL_PERFORMANCE_BLOCK_LOW_WIN_RATE', False)
SYMBOL_PERFORMANCE_MIN_TRADES = _env_int('SYMBOL_PERFORMANCE_MIN_TRADES', 3)
SYMBOL_PERFORMANCE_MIN_WIN_RATE = _env_float('SYMBOL_PERFORMANCE_MIN_WIN_RATE', 0.45)
SYMBOL_PERFORMANCE_MIN_AVG_PNL = _env_float('SYMBOL_PERFORMANCE_MIN_AVG_PNL', -1500.0)


def _normalize_symbol(symbol: Any) -> str:
    s = str(symbol or '').strip().upper()
    if s.endswith('.T'):
        s = s[:-2]
    if s.endswith('.0') and s[:-2].isdigit():
        s = s[:-2]
    return s


def _to_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    return s in {'1', 'true', 'yes', 'y', 'on'}


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


@lru_cache(maxsize=8192)
def load_symbol_performance(symbol: str, db_path: str = SYMBOL_PERFORMANCE_DB_PATH) -> dict:
    symbol = _normalize_symbol(symbol)
    if not symbol:
        return {}
    if not os.path.exists(db_path):
        return {}

    try:
        with sqlite3.connect(db_path, timeout=10) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                'SELECT * FROM symbol_performance_latest WHERE symbol=? LIMIT 1',
                (symbol,),
            )
            row = cur.fetchone()
            if not row:
                return {}
            return dict(row)
    except Exception:
        logger.debug('[SYMBOL PERF GUARD] load failed symbol=%s db=%s', symbol, db_path, exc_info=True)
        return {}


def clear_symbol_performance_cache() -> None:
    load_symbol_performance.cache_clear()


def is_symbol_performance_ok(symbol: str, *, strict: bool | None = None) -> tuple[bool, dict]:
    """銘柄別統計でENTRY可能か判定する。"""
    if not ENABLE_SYMBOL_PERFORMANCE_GUARD:
        return True, {'reason': 'SYMBOL_PERFORMANCE_GUARD_DISABLED'}

    symbol = _normalize_symbol(symbol)
    strict_mode = SYMBOL_PERFORMANCE_STRICT_BLACKLIST if strict is None else bool(strict)
    perf = load_symbol_performance(symbol)

    if not perf:
        return True, {
            'symbol': symbol,
            'reason': 'NO_SYMBOL_PERFORMANCE_DATA',
            'ok': True,
            'strict': strict_mode,
        }

    trades = _safe_int(perf.get('trades'), 0)
    win_rate = _safe_float(perf.get('win_rate'), 0.0)
    avg_pnl = _safe_float(perf.get('avg_pnl'), 0.0)
    net_pnl = _safe_float(perf.get('net_pnl'), 0.0)
    blacklist = _to_bool(perf.get('blacklist'))
    allow_entry = _to_bool(perf.get('allow_entry'))
    insufficient = _to_bool(perf.get('insufficient_samples')) or trades < SYMBOL_PERFORMANCE_MIN_TRADES

    detail = {
        'symbol': symbol,
        'trades': trades,
        'win_rate': win_rate,
        'avg_pnl': avg_pnl,
        'net_pnl': net_pnl,
        'blacklist': blacklist,
        'allow_entry': allow_entry,
        'insufficient_samples': insufficient,
        'strict_blacklist': strict_mode,
        'min_trades': SYMBOL_PERFORMANCE_MIN_TRADES,
        'min_win_rate': SYMBOL_PERFORMANCE_MIN_WIN_RATE,
        'min_avg_pnl': SYMBOL_PERFORMANCE_MIN_AVG_PNL,
    }

    if blacklist and strict_mode:
        detail['reason'] = 'SYMBOL_BLACKLIST'
        detail['ok'] = False
        logger.warning('[SYMBOL PERF GUARD] NG symbol=%s detail=%s', symbol, detail)
        return False, detail

    if insufficient:
        detail['reason'] = 'INSUFFICIENT_SAMPLES_ALLOW'
        detail['ok'] = True
        logger.info('[SYMBOL PERF GUARD] ALLOW symbol=%s detail=%s', symbol, detail)
        return True, detail

    if SYMBOL_PERFORMANCE_BLOCK_LOW_WIN_RATE and win_rate < SYMBOL_PERFORMANCE_MIN_WIN_RATE:
        detail['reason'] = 'LOW_WIN_RATE'
        detail['ok'] = False
        logger.warning('[SYMBOL PERF GUARD] NG symbol=%s detail=%s', symbol, detail)
        return False, detail

    if avg_pnl < SYMBOL_PERFORMANCE_MIN_AVG_PNL:
        detail['reason'] = 'AVG_PNL_TOO_LOW'
        detail['ok'] = False
        logger.warning('[SYMBOL PERF GUARD] NG symbol=%s detail=%s', symbol, detail)
        return False, detail

    detail['reason'] = 'OK' if allow_entry else 'NOT_ALLOW_ENTRY_BUT_NOT_BLOCKED'
    detail['ok'] = True
    logger.info('[SYMBOL PERF GUARD] OK symbol=%s detail=%s', symbol, detail)
    return True, detail
