# ============================================================
# File   : trading/audit_logging/fill_cancel_audit.py
# Version: Ver01-FILL-CANCEL-AUDIT
# ------------------------------------------------------------
# 約定検知・取消検知・10秒未約定取消を保存する補助モジュール。
# 既存の約定照会/取消処理から1行で呼べるようにする。
# ============================================================

from __future__ import annotations

from datetime import datetime
from typing import Any

from trading.audit_logging.order_exit_audit import (
    audit_order_filled,
    audit_order_cancelled,
    audit_order_timeout_cancel,
)


def _get(d: Any, *keys: str, default=None):
    if not isinstance(d, dict):
        return default
    for k in keys:
        if k in d and d.get(k) not in (None, ''):
            return d.get(k)
    return default


def audit_fill_from_response(response: dict, *, default_symbol: str = '', default_side: str = '') -> None:
    """
    kabu API の約定/注文照会レスポンスらしき dict から約定情報を保存する。
    キー名の揺れに耐えるため複数候補を読む。
    """
    try:
        symbol = _get(response, 'Symbol', 'symbol', 'Code', 'code', default=default_symbol)
        side = _get(response, 'Side', 'side', 'BuySell', 'buy_sell', default=default_side)
        order_id = _get(response, 'OrderId', 'order_id', 'ID', 'id', default='')
        qty = _get(response, 'CumQty', 'ExecutedQty', 'Qty', 'qty', 'filled_qty', default=0)
        order_price = _get(response, 'Price', 'price', 'OrderPrice', default=0)
        filled_price = _get(response, 'ExecutionPrice', 'FilledPrice', 'filled_price', 'AvgPrice', default=order_price)
        order_type = _get(response, 'FrontOrderType', 'order_type', default='')

        side_text = str(side)
        if side_text == '2':
            side_text = 'BUY'
        elif side_text == '1':
            side_text = 'SELL'

        audit_order_filled(
            symbol=str(symbol),
            side=side_text,
            qty=qty,
            order_type=str(order_type),
            order_id=order_id,
            price=order_price,
            filled_price=filled_price,
            detail={'source': 'audit_fill_from_response', 'response': response},
        )
    except Exception:
        return


def audit_cancel_from_response(response: dict, *, default_symbol: str = '', default_side: str = '', reason: str = '') -> None:
    """取消完了レスポンスらしき dict から取消履歴を保存する。"""
    try:
        symbol = _get(response, 'Symbol', 'symbol', 'Code', 'code', default=default_symbol)
        side = _get(response, 'Side', 'side', 'BuySell', 'buy_sell', default=default_side)
        order_id = _get(response, 'OrderId', 'order_id', 'ID', 'id', default='')
        qty = _get(response, 'Qty', 'qty', 'CancelQty', default=0)
        order_type = _get(response, 'FrontOrderType', 'order_type', default='')

        side_text = str(side)
        if side_text == '2':
            side_text = 'BUY'
        elif side_text == '1':
            side_text = 'SELL'

        audit_order_cancelled(
            symbol=str(symbol),
            side=side_text,
            qty=qty,
            order_type=str(order_type),
            order_id=order_id,
            reason=reason or 'cancel_response',
            detail=response,
        )
    except Exception:
        return


def audit_entry_timeout_cancel(symbol: str, side: str, order_id: str, qty: Any = 0, timeout_sec: int = 10, detail: Any = None) -> None:
    """エントリー注文が指定秒数で約定しなかった取消を保存する。"""
    try:
        audit_order_timeout_cancel(
            symbol=symbol,
            side=side,
            qty=qty,
            order_id=order_id,
            timeout_sec=timeout_sec,
            detail={
                'source': 'entry_timeout_cancel',
                'timeout_sec': timeout_sec,
                'detail': detail,
                'audit_time': datetime.now().isoformat(timespec='seconds'),
            },
        )
    except Exception:
        return
