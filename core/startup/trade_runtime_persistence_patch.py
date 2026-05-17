# ============================================================
# File   : core/startup/trade_runtime_persistence_patch.py
# Version: Ver01-TRADE-RUNTIME-PERSISTENCE-PATCH
# ------------------------------------------------------------
# 売買に関する runtime 証跡を保存する runtime patch。
# 既存の注文ロジックを直接壊さず、以下を monkey patch する。
#
# 対象:
#   - kabu_api.buy_sell_entry._send_order
#       新規 entry 注文送信後に pending_orders_runtime へ保存
#   - kabu_api.close.process_exit
#       exit 注文送信後に pending_orders_runtime へ保存
#       成功時に positions_runtime を CLOSED 扱いへ補正
#
# 保存先:
#   \\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\runtime_state\runtime_state_YYYYMMDD.db
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

logger = logging.getLogger(__name__)

_INSTALLED = False
_ORIGINAL_SEND_ORDER = None
_ORIGINAL_PROCESS_EXIT = None


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


def _side_from_payload(payload: dict | None) -> str:
    if not isinstance(payload, dict):
        return ''
    side = str(payload.get('Side') or payload.get('side') or '')
    if side == '2':
        return 'BUY'
    if side == '1':
        return 'SELL'
    return side.upper()


def _symbol_from_position(pos: Any) -> str:
    try:
        if isinstance(pos, dict):
            return str(pos.get('symbol') or pos.get('Symbol') or pos.get('stock_code') or '').strip()
        return str(getattr(pos, 'symbol', '') or getattr(pos, 'Symbol', '') or getattr(pos, 'stock_code', '') or '').strip()
    except Exception:
        return ''


def _side_from_position(pos: Any) -> str:
    try:
        if isinstance(pos, dict):
            s = str(pos.get('side') or pos.get('Side') or '').upper()
        else:
            s = str(getattr(pos, 'side', '') or getattr(pos, 'Side', '') or '').upper()
        if s.startswith('SHORT'):
            return 'SELL'
        if s.startswith('SELL'):
            return 'SELL'
        if s.startswith('BUY'):
            return 'BUY'
        return s or 'BUY'
    except Exception:
        return 'BUY'


def _entry_time_from_position(pos: Any) -> str:
    try:
        if isinstance(pos, dict):
            v = pos.get('entry_time') or pos.get('EntryTime') or pos.get('created_at')
        else:
            v = getattr(pos, 'entry_time', None) or getattr(pos, 'EntryTime', None) or getattr(pos, 'created_at', None)
        if isinstance(v, dt.datetime):
            return v.isoformat(timespec='seconds')
        if v:
            return str(v)
    except Exception:
        pass
    return ''


def _save_pending_order(order_id: str, symbol: str, side: str, qty: Any, *, status: str, seconds_to_cancel: int = 10, payload: dict | None = None) -> None:
    try:
        from trading.runtime_persistence.runtime_state_store import save_pending_order_state

        now = dt.datetime.now()
        deadline = now + dt.timedelta(seconds=seconds_to_cancel)
        save_pending_order_state(
            order_id=str(order_id),
            symbol=str(symbol),
            side=str(side),
            send_time=now.isoformat(timespec='seconds'),
            cancel_deadline=deadline.isoformat(timespec='seconds'),
            qty=_safe_int(qty, 0),
            status=status,
        )
        logger.warning(
            '[TRADE RUNTIME] pending order saved order_id=%s symbol=%s side=%s qty=%s status=%s',
            order_id,
            symbol,
            side,
            qty,
            status,
        )
    except Exception:
        logger.exception('[TRADE RUNTIME] save pending order failed order_id=%s symbol=%s', order_id, symbol)


def _mark_position_closed_from_exit(pos: Any) -> None:
    try:
        from trading.runtime_persistence.runtime_state_store import mark_position_closed

        symbol = _symbol_from_position(pos)
        side = _side_from_position(pos)
        entry_time = _entry_time_from_position(pos)
        if not symbol or not entry_time:
            return
        mark_position_closed(symbol=symbol, side=side, entry_time=entry_time)
        logger.warning('[TRADE RUNTIME] position marked closed symbol=%s side=%s entry_time=%s', symbol, side, entry_time)
    except Exception:
        logger.exception('[TRADE RUNTIME] mark position closed failed')


def _patch_buy_sell_entry() -> bool:
    global _ORIGINAL_SEND_ORDER
    try:
        import kabu_api.buy_sell_entry as bse

        original = getattr(bse, '_send_order', None)
        if not callable(original):
            logger.warning('[TRADE RUNTIME PATCH] buy_sell_entry._send_order not callable')
            return False

        if getattr(original, '_trade_runtime_persistence_patched', False):
            return True

        _ORIGINAL_SEND_ORDER = original

        def _wrapped_send_order(payload, symbol):
            res = original(payload, symbol)
            try:
                if isinstance(res, dict) and res.get('OrderId'):
                    side = _side_from_payload(payload)
                    qty = _safe_int(payload.get('Qty') if isinstance(payload, dict) else 0, 0)
                    _save_pending_order(
                        order_id=str(res.get('OrderId')),
                        symbol=str(symbol),
                        side=side,
                        qty=qty,
                        status='PENDING_ENTRY',
                        seconds_to_cancel=10,
                        payload=payload if isinstance(payload, dict) else None,
                    )
            except Exception:
                logger.exception('[TRADE RUNTIME PATCH] entry order persistence failed symbol=%s res=%s', symbol, res)
            return res

        _wrapped_send_order._trade_runtime_persistence_patched = True
        bse._send_order = _wrapped_send_order
        logger.warning('[TRADE RUNTIME PATCH] patched kabu_api.buy_sell_entry._send_order')
        return True

    except Exception:
        logger.exception('[TRADE RUNTIME PATCH] patch buy_sell_entry failed')
        return False


def _patch_process_exit() -> bool:
    global _ORIGINAL_PROCESS_EXIT
    try:
        import kabu_api.close as close_mod

        original = getattr(close_mod, 'process_exit', None)
        if not callable(original):
            logger.warning('[TRADE RUNTIME PATCH] kabu_api.close.process_exit not callable')
            return False

        if getattr(original, '_trade_runtime_persistence_patched', False):
            return True

        _ORIGINAL_PROCESS_EXIT = original

        def _wrapped_process_exit(pos, exit_price, reason):
            api = original(pos, exit_price, reason)
            try:
                if isinstance(api, dict) and api.get('order_id'):
                    symbol = _symbol_from_position(pos)
                    side = _side_from_position(pos)
                    qty = 0
                    try:
                        if isinstance(pos, dict):
                            qty = pos.get('qty') or pos.get('Qty') or pos.get('quantity')
                        else:
                            qty = getattr(pos, 'qty', None) or getattr(pos, 'Qty', None) or getattr(pos, 'quantity', None)
                    except Exception:
                        qty = 0

                    _save_pending_order(
                        order_id=str(api.get('order_id')),
                        symbol=symbol,
                        side='EXIT_' + side,
                        qty=qty,
                        status='PENDING_EXIT',
                        seconds_to_cancel=10,
                    )
                    _mark_position_closed_from_exit(pos)
            except Exception:
                logger.exception('[TRADE RUNTIME PATCH] exit order persistence failed api=%s', api)
            return api

        _wrapped_process_exit._trade_runtime_persistence_patched = True
        close_mod.process_exit = _wrapped_process_exit
        logger.warning('[TRADE RUNTIME PATCH] patched kabu_api.close.process_exit')
        return True

    except Exception:
        logger.exception('[TRADE RUNTIME PATCH] patch process_exit failed')
        return False


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True

    ok_entry = _patch_buy_sell_entry()
    ok_exit = _patch_process_exit()
    _INSTALLED = bool(ok_entry or ok_exit)

    logger.warning(
        '[TRADE RUNTIME PATCH] install done ok=%s entry=%s exit=%s',
        _INSTALLED,
        ok_entry,
        ok_exit,
    )
    return _INSTALLED
