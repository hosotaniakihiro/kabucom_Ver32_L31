# ============================================================
# File   : trading/audit_logging/buy_sell_entry_audit_patch.py
# Version: Ver01-BUY-SELL-ENTRY-AUDIT-PATCH
# ------------------------------------------------------------
# kabu_api.buy_sell_entry の _send_order を安全にパッチし、
# 発注送信・API失敗・OrderId取得を audit DB に保存する。
# 売買ロジックとpayloadは変更しない。
# ============================================================

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_INSTALLED = False
_ORIGINAL_SEND_ORDER = None


def _safe_get(payload: Any, key: str, default=None):
    try:
        if isinstance(payload, dict):
            return payload.get(key, default)
    except Exception:
        pass
    return default


def _side_from_payload(payload: dict) -> str:
    try:
        side = int(payload.get('Side'))
        if side == 2:
            return 'BUY'
        if side == 1:
            return 'SELL'
    except Exception:
        pass
    return ''


def _order_type_from_payload(payload: dict) -> str:
    try:
        t = int(payload.get('FrontOrderType'))
        if t == 10:
            return 'MARKET'
        if t == 20:
            return 'LIMIT'
        if t == 30:
            return 'STOP'
    except Exception:
        pass
    return str(payload.get('FrontOrderType') or '')


def install() -> bool:
    global _INSTALLED, _ORIGINAL_SEND_ORDER

    if _INSTALLED:
        return True

    try:
        from kabu_api import buy_sell_entry as bse
        from trading.audit_logging.order_exit_audit import audit_order_sent
    except Exception:
        logger.exception('[AUDIT PATCH] buy_sell_entry import failed')
        return False

    if getattr(bse, '_AUDIT_LOGGING_PATCH_INSTALLED', False):
        _INSTALLED = True
        return True

    _ORIGINAL_SEND_ORDER = bse._send_order

    def patched_send_order(payload, symbol):
        side = _side_from_payload(payload if isinstance(payload, dict) else {})
        order_type = _order_type_from_payload(payload if isinstance(payload, dict) else {})
        qty = _safe_get(payload, 'Qty', 0)
        price = _safe_get(payload, 'Price', 0)

        try:
            audit_order_sent(
                symbol=symbol,
                side=side,
                qty=qty,
                order_type=order_type,
                price=price,
                order_id='',
                detail={'phase': 'before_send_order_common'},
            )
        except Exception:
            pass

        res = _ORIGINAL_SEND_ORDER(payload, symbol)

        try:
            if isinstance(res, dict) and res.get('OrderId'):
                audit_order_sent(
                    symbol=symbol,
                    side=side,
                    qty=qty,
                    order_type=order_type,
                    price=res.get('Price', price),
                    order_id=res.get('OrderId'),
                    detail={'phase': 'order_id_received', 'response': res},
                )
            else:
                audit_order_sent(
                    symbol=symbol,
                    side=side,
                    qty=qty,
                    order_type=order_type,
                    price=price,
                    order_id='',
                    detail={'phase': 'send_order_failed_or_no_order_id', 'response': res},
                )
        except Exception:
            pass

        return res

    bse._send_order = patched_send_order
    bse._AUDIT_LOGGING_PATCH_INSTALLED = True
    _INSTALLED = True

    logger.warning('[AUDIT PATCH] buy_sell_entry order audit logging installed')
    return True
