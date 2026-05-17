# ============================================================
# File   : trading/entry/spread_guard.py
# Version: Ver01-ENTRY-SPREAD-GUARD
# ------------------------------------------------------------
# エントリー前のスプレッド安全弁。
# 2500〜7000円・70万円運用では板飛びの損失が大きくなるため、
# bid/ask spread が広い銘柄を新規エントリーから除外する。
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Any

from utils_common import get_latest_bid_ask

logger = logging.getLogger(__name__)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.environ.get(name)
        if v is None or str(v).strip() == '':
            return float(default)
        return float(str(v).strip())
    except Exception:
        return float(default)


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


ENABLE_ENTRY_SPREAD_GUARD = _env_bool('ENABLE_ENTRY_SPREAD_GUARD', True)
ENTRY_MAX_SPREAD_PCT = _env_float('ENTRY_MAX_SPREAD_PCT', 0.20)
ENTRY_MAX_SPREAD_YEN = _env_float('ENTRY_MAX_SPREAD_YEN', 15.0)


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == '':
            return default
        return float(v)
    except Exception:
        return default


def calc_spread_metrics(quotes: dict | None) -> dict:
    if not isinstance(quotes, dict):
        return {
            'ok': False,
            'reason': 'NO_QUOTES',
            'bid': 0.0,
            'ask': 0.0,
            'spread': 0.0,
            'spread_pct': 999.0,
        }

    bid = _safe_float(
        quotes.get('bid_price') or quotes.get('BidPrice') or quotes.get('bid'),
        0.0,
    )
    ask = _safe_float(
        quotes.get('ask_price') or quotes.get('AskPrice') or quotes.get('ask'),
        0.0,
    )

    if bid <= 0 or ask <= 0:
        return {
            'ok': False,
            'reason': 'INVALID_BID_ASK',
            'bid': bid,
            'ask': ask,
            'spread': 0.0,
            'spread_pct': 999.0,
        }

    spread = ask - bid
    mid = (ask + bid) / 2.0
    spread_pct = spread / mid * 100.0 if mid > 0 else 999.0

    return {
        'ok': True,
        'reason': 'OK',
        'bid': bid,
        'ask': ask,
        'spread': spread,
        'spread_pct': spread_pct,
    }


def is_spread_acceptable(symbol: str, quotes: dict | None = None, *, max_spread_pct: float | None = None, max_spread_yen: float | None = None) -> tuple[bool, dict]:
    """spread が許容範囲なら True。"""
    if not ENABLE_ENTRY_SPREAD_GUARD:
        return True, {'reason': 'SPREAD_GUARD_DISABLED'}

    try:
        if quotes is None:
            quotes = get_latest_bid_ask(symbol)

        m = calc_spread_metrics(quotes)
        max_pct = ENTRY_MAX_SPREAD_PCT if max_spread_pct is None else float(max_spread_pct)
        max_yen = ENTRY_MAX_SPREAD_YEN if max_spread_yen is None else float(max_spread_yen)

        m['max_spread_pct'] = max_pct
        m['max_spread_yen'] = max_yen

        if not m.get('ok'):
            logger.warning('[SPREAD GUARD] NG symbol=%s reason=%s metrics=%s', symbol, m.get('reason'), m)
            return False, m

        if m['spread'] > max_yen:
            m['reason'] = 'SPREAD_YEN_TOO_WIDE'
            logger.warning('[SPREAD GUARD] NG symbol=%s metrics=%s', symbol, m)
            return False, m

        if m['spread_pct'] > max_pct:
            m['reason'] = 'SPREAD_PCT_TOO_WIDE'
            logger.warning('[SPREAD GUARD] NG symbol=%s metrics=%s', symbol, m)
            return False, m

        logger.info('[SPREAD GUARD] OK symbol=%s metrics=%s', symbol, m)
        return True, m

    except Exception as e:
        detail = {'reason': 'SPREAD_GUARD_EXCEPTION', 'error': str(e)}
        logger.exception('[SPREAD GUARD] exception symbol=%s', symbol)
        return False, detail


def assert_spread_or_skip(symbol: str, quotes: dict | None = None) -> bool:
    ok, _ = is_spread_acceptable(symbol, quotes=quotes)
    return ok
