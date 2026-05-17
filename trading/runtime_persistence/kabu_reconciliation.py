# ============================================================
# File   : trading/runtime_persistence/kabu_reconciliation.py
# Version: Ver01-KABU-RUNTIME-RECONCILIATION
# ------------------------------------------------------------
# 再起動後に kabu API の実建玉と runtime_state.db を突き合わせる。
# 実建玉を正とし、runtime_state の OPEN positions を復元/補正する。
#
# 使い方:
#   positions = get_positions_somehow()
#   result = reconcile_kabu_positions(positions)
#
# このモジュール自体は注文を出さない。DB同期のみ。
# ============================================================

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from .runtime_state_store import (
    load_open_positions,
    save_position_state,
    mark_position_closed,
)

logger = logging.getLogger(__name__)


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


def _get(d: dict, *keys: str, default=None):
    if not isinstance(d, dict):
        return default
    for k in keys:
        if k in d and d.get(k) not in (None, ''):
            return d.get(k)
    return default


def normalize_kabu_position(raw: dict) -> dict:
    """kabu API 建玉レスポンスのキー揺れを吸収して標準化する。"""
    symbol = str(_get(raw, 'Symbol', 'symbol', 'Code', 'code', default='')).strip()

    side_raw = _get(raw, 'Side', 'side', 'BuySell', 'buy_sell', default='')
    side = str(side_raw).upper()
    if side in {'2', 'BUY', '買', 'LONG'}:
        side = 'BUY'
    elif side in {'1', 'SELL', '売', 'SHORT'}:
        side = 'SELL'

    qty = _safe_int(_get(raw, 'LeavesQty', 'HoldQty', 'Qty', 'qty', 'Quantity', default=0), 0)
    entry_price = _safe_float(_get(raw, 'Price', 'AvgPrice', 'EntryPrice', 'ExecutionPrice', 'price', 'entry_price', default=0), 0.0)
    current_price = _safe_float(_get(raw, 'CurrentPrice', 'current_price', 'MarketPrice', default=entry_price), entry_price)
    entry_time = _get(raw, 'ExecutionDay', 'EntryTime', 'entry_time', 'DateTime', default='')
    if not entry_time:
        entry_time = datetime.now().isoformat(timespec='seconds')

    return {
        'symbol': symbol,
        'side': side,
        'qty': qty,
        'entry_price': entry_price,
        'current_price': current_price,
        'entry_time': str(entry_time),
        'raw': raw,
    }


def _key(pos: dict) -> tuple[str, str]:
    return (str(pos.get('symbol') or ''), str(pos.get('side') or '').upper())


def reconcile_kabu_positions(kabu_positions: list[dict] | None, *, trade_date: str | None = None) -> dict:
    """
    kabu実建玉を正として runtime_state を補正する。

    - kabuにあるがruntimeにない → OPENとして追加
    - runtimeにあるがkabuにない → CLOSEDへ変更
    - 両方にある → highest/lowestを壊さないよう現在価格だけ更新
    """
    kabu_positions = kabu_positions or []
    normalized = [normalize_kabu_position(x) for x in kabu_positions if isinstance(x, dict)]
    normalized = [x for x in normalized if x.get('symbol') and x.get('side') in ('BUY', 'SELL') and _safe_int(x.get('qty'), 0) > 0]

    runtime_open = load_open_positions(trade_date=trade_date)
    runtime_map = {_key(x): x for x in runtime_open}
    kabu_map = {_key(x): x for x in normalized}

    added = []
    updated = []
    closed = []

    # kabu実建玉を runtime へ反映
    for k, kp in kabu_map.items():
        rp = runtime_map.get(k)
        entry_price = _safe_float(kp.get('entry_price'), 0.0)
        current_price = _safe_float(kp.get('current_price'), entry_price)

        if rp:
            high = max(
                _safe_float(rp.get('highest_since_entry'), entry_price),
                current_price,
                entry_price,
            )
            low_base = _safe_float(rp.get('lowest_since_entry'), entry_price)
            if low_base <= 0:
                low_base = entry_price
            low = min(low_base, current_price, entry_price)
            entry_time = rp.get('entry_time') or kp.get('entry_time')
            updated.append({'symbol': k[0], 'side': k[1], 'action': 'updated'})
        else:
            high = max(entry_price, current_price)
            low = min(entry_price, current_price) if entry_price > 0 and current_price > 0 else entry_price
            entry_time = kp.get('entry_time')
            added.append({'symbol': k[0], 'side': k[1], 'action': 'added_from_kabu'})

        save_position_state(
            symbol=kp.get('symbol'),
            side=kp.get('side'),
            entry_time=entry_time,
            entry_price=entry_price,
            current_price=current_price,
            highest_since_entry=high,
            lowest_since_entry=low,
            qty=_safe_int(kp.get('qty'), 0),
            status='OPEN',
            trade_date=trade_date,
        )

    # runtimeにあるがkabuにないものは閉じる
    for k, rp in runtime_map.items():
        if k not in kabu_map:
            mark_position_closed(
                symbol=rp.get('symbol'),
                side=rp.get('side'),
                entry_time=rp.get('entry_time'),
                trade_date=trade_date,
            )
            closed.append({'symbol': k[0], 'side': k[1], 'action': 'closed_not_in_kabu'})

    result = {
        'trade_date': trade_date,
        'kabu_positions': len(normalized),
        'runtime_open_before': len(runtime_open),
        'added': added,
        'updated': updated,
        'closed': closed,
        'reconciled_at': datetime.now().isoformat(timespec='seconds'),
    }

    logger.warning('[KABU RECONCILE] %s', result)
    return result
