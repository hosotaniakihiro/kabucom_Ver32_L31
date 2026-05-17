# ============================================================
# File   : trading/entry/order_book_imbalance_guard.py
# Version: Ver01-ORDER-BOOK-IMBALANCE-GUARD
# ------------------------------------------------------------
# エントリー前に板の買い/売り優勢を確認する安全弁。
# BUYなら bid側が厚い、SELLなら ask側が厚い状態を優先する。
#
# 2500〜7000円・70万円運用では板の薄さ/偏りで滑りやすいため、
# spread_guard / entry_quality_score と併用する。
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


ENABLE_ORDER_BOOK_IMBALANCE_GUARD = _env_bool('ENABLE_ORDER_BOOK_IMBALANCE_GUARD', True)

# BUY時: bid_qty / ask_qty がこの値以上なら買い板優勢。
# SELL時: ask_qty / bid_qty がこの値以上なら売り板優勢。
ORDER_BOOK_MIN_IMBALANCE_RATIO = _env_float('ORDER_BOOK_MIN_IMBALANCE_RATIO', 1.15)

# 片側板数量が小さすぎる銘柄を落とす。
ORDER_BOOK_MIN_SIDE_QTY = _env_float('ORDER_BOOK_MIN_SIDE_QTY', 100.0)


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == '':
            return default
        return float(v)
    except Exception:
        return default


def _pick_first(d: dict, keys: tuple[str, ...], default: Any = None):
    for k in keys:
        if k in d and d.get(k) not in (None, ''):
            return d.get(k)
    return default


def calc_order_book_imbalance(quotes: dict | None) -> dict:
    """
    get_latest_bid_ask の戻り値、または板情報dictから imbalance を計算する。
    キー揺れに耐える。
    """
    if not isinstance(quotes, dict):
        return {
            'ok': False,
            'reason': 'NO_QUOTES',
            'bid_qty': 0.0,
            'ask_qty': 0.0,
            'bid_ask_ratio': 0.0,
            'ask_bid_ratio': 0.0,
        }

    bid_qty = _safe_float(_pick_first(quotes, ('bid_qty', 'BidQty', 'bid_volume', 'BidVolume', '売買高_買気配数量'), 0.0), 0.0)
    ask_qty = _safe_float(_pick_first(quotes, ('ask_qty', 'AskQty', 'ask_volume', 'AskVolume', '売買高_売気配数量'), 0.0), 0.0)

    # 板5本/10本などを持っている場合の簡易対応
    # bids=[{'qty':...}], asks=[{'qty':...}] のような形式も読む。
    if bid_qty <= 0:
        bids = quotes.get('bids') or quotes.get('Bids') or []
        try:
            if isinstance(bids, list):
                bid_qty = sum(_safe_float(x.get('qty') or x.get('Qty') or x.get('volume') or x.get('Volume'), 0.0) for x in bids if isinstance(x, dict))
        except Exception:
            bid_qty = 0.0

    if ask_qty <= 0:
        asks = quotes.get('asks') or quotes.get('Asks') or []
        try:
            if isinstance(asks, list):
                ask_qty = sum(_safe_float(x.get('qty') or x.get('Qty') or x.get('volume') or x.get('Volume'), 0.0) for x in asks if isinstance(x, dict))
        except Exception:
            ask_qty = 0.0

    bid_ask_ratio = bid_qty / ask_qty if ask_qty > 0 else 0.0
    ask_bid_ratio = ask_qty / bid_qty if bid_qty > 0 else 0.0

    return {
        'ok': bid_qty > 0 and ask_qty > 0,
        'reason': 'OK' if bid_qty > 0 and ask_qty > 0 else 'MISSING_SIDE_QTY',
        'bid_qty': bid_qty,
        'ask_qty': ask_qty,
        'bid_ask_ratio': bid_ask_ratio,
        'ask_bid_ratio': ask_bid_ratio,
    }


def is_order_book_imbalance_ok(symbol: str, side: str, quotes: dict | None = None) -> tuple[bool, dict]:
    """side方向に板の厚みがあるか判定する。"""
    if not ENABLE_ORDER_BOOK_IMBALANCE_GUARD:
        return True, {'reason': 'ORDER_BOOK_IMBALANCE_GUARD_DISABLED'}

    try:
        if quotes is None:
            quotes = get_latest_bid_ask(symbol)

        m = calc_order_book_imbalance(quotes)
        side_u = str(side or '').upper()
        m['symbol'] = str(symbol)
        m['side'] = side_u
        m['min_ratio'] = ORDER_BOOK_MIN_IMBALANCE_RATIO
        m['min_side_qty'] = ORDER_BOOK_MIN_SIDE_QTY

        if not m.get('ok'):
            logger.warning('[ORDER BOOK GUARD] NG symbol=%s side=%s metrics=%s', symbol, side_u, m)
            return False, m

        if m['bid_qty'] < ORDER_BOOK_MIN_SIDE_QTY or m['ask_qty'] < ORDER_BOOK_MIN_SIDE_QTY:
            m['reason'] = 'SIDE_QTY_TOO_SMALL'
            logger.warning('[ORDER BOOK GUARD] NG symbol=%s side=%s metrics=%s', symbol, side_u, m)
            return False, m

        if side_u == 'BUY':
            if m['bid_ask_ratio'] < ORDER_BOOK_MIN_IMBALANCE_RATIO:
                m['reason'] = 'BUY_NOT_BID_DOMINANT'
                logger.warning('[ORDER BOOK GUARD] NG symbol=%s side=%s metrics=%s', symbol, side_u, m)
                return False, m

        elif side_u == 'SELL':
            if m['ask_bid_ratio'] < ORDER_BOOK_MIN_IMBALANCE_RATIO:
                m['reason'] = 'SELL_NOT_ASK_DOMINANT'
                logger.warning('[ORDER BOOK GUARD] NG symbol=%s side=%s metrics=%s', symbol, side_u, m)
                return False, m

        else:
            m['reason'] = 'UNKNOWN_SIDE'
            logger.warning('[ORDER BOOK GUARD] NG symbol=%s side=%s metrics=%s', symbol, side_u, m)
            return False, m

        m['reason'] = 'OK'
        logger.info('[ORDER BOOK GUARD] OK symbol=%s side=%s metrics=%s', symbol, side_u, m)
        return True, m

    except Exception as e:
        detail = {'reason': 'ORDER_BOOK_GUARD_EXCEPTION', 'error': str(e), 'symbol': str(symbol), 'side': str(side)}
        logger.exception('[ORDER BOOK GUARD] exception symbol=%s side=%s', symbol, side)
        return False, detail
