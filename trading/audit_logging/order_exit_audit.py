# ============================================================
# File   : trading/audit_logging/order_exit_audit.py
# Version: Ver01-ORDER-EXIT-AUDIT-HELPERS
# ------------------------------------------------------------
# 約定・取消・イグジット判定をバックテスト用DBへ保存する共通部品。
# 監査保存に失敗しても売買処理を止めない。
# ============================================================

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from .recorder import (
    record_order_event,
    record_exit_decision,
    record_position_state,
)


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


def _safe_text(v: Any, max_len: int = 2000) -> str:
    try:
        if isinstance(v, str):
            s = v
        else:
            s = json.dumps(v, ensure_ascii=False, default=str)
    except Exception:
        s = str(v)
    return s[:max_len]


def audit_order_sent(symbol: str, side: str, qty: Any, order_type: str, price: Any, order_id: Any = None, detail: Any = None) -> None:
    try:
        record_order_event(
            datetime=datetime.now().isoformat(timespec='seconds'),
            symbol=str(symbol),
            side=str(side).upper(),
            qty=_safe_int(qty, 0),
            order_type=str(order_type or ''),
            order_id=str(order_id or ''),
            status='SENT',
            price=_safe_float(price, 0.0),
            filled_price=None,
            cancel_reason=_safe_text(detail or ''),
        )
    except Exception:
        return


def audit_order_filled(symbol: str, side: str, qty: Any, order_type: str = '', order_id: Any = None, price: Any = None, filled_price: Any = None, detail: Any = None) -> None:
    try:
        record_order_event(
            datetime=datetime.now().isoformat(timespec='seconds'),
            symbol=str(symbol),
            side=str(side).upper(),
            qty=_safe_int(qty, 0),
            order_type=str(order_type or ''),
            order_id=str(order_id or ''),
            status='FILLED',
            price=_safe_float(price, 0.0),
            filled_price=_safe_float(filled_price, 0.0),
            cancel_reason=_safe_text(detail or ''),
        )
    except Exception:
        return


def audit_order_cancelled(symbol: str, side: str, qty: Any = 0, order_type: str = '', order_id: Any = None, reason: str = '', detail: Any = None) -> None:
    try:
        record_order_event(
            datetime=datetime.now().isoformat(timespec='seconds'),
            symbol=str(symbol),
            side=str(side).upper(),
            qty=_safe_int(qty, 0),
            order_type=str(order_type or ''),
            order_id=str(order_id or ''),
            status='CANCELLED',
            price=None,
            filled_price=None,
            cancel_reason=_safe_text({'reason': reason, 'detail': detail}),
        )
    except Exception:
        return


def audit_order_timeout_cancel(symbol: str, side: str, qty: Any = 0, order_id: Any = None, timeout_sec: Any = 10, detail: Any = None) -> None:
    try:
        audit_order_cancelled(
            symbol=symbol,
            side=side,
            qty=qty,
            order_type='ENTRY_TIMEOUT_CANCEL',
            order_id=order_id,
            reason=f'entry_not_filled_within_{timeout_sec}s',
            detail=detail,
        )
    except Exception:
        return


def audit_exit_check(symbol: str, side: str, entry_price: Any, current_price: Any, highest_since_entry: Any = None, lowest_since_entry: Any = None, exit_reason: str = '', triggered: bool = False) -> None:
    try:
        record_exit_decision(
            datetime=datetime.now().isoformat(timespec='seconds'),
            symbol=str(symbol),
            side=str(side).upper(),
            entry_price=_safe_float(entry_price, 0.0),
            current_price=_safe_float(current_price, 0.0),
            highest_since_entry=_safe_float(highest_since_entry, 0.0),
            lowest_since_entry=_safe_float(lowest_since_entry, 0.0),
            exit_reason=str(exit_reason or ''),
            triggered=bool(triggered),
        )
    except Exception:
        return


def audit_position_snapshot(symbol: str, side: str, qty: Any, entry_price: Any, highest_since_entry: Any = None, lowest_since_entry: Any = None, holding_seconds: Any = None) -> None:
    try:
        record_position_state(
            datetime=datetime.now().isoformat(timespec='seconds'),
            symbol=str(symbol),
            side=str(side).upper(),
            qty=_safe_int(qty, 0),
            entry_price=_safe_float(entry_price, 0.0),
            highest_since_entry=_safe_float(highest_since_entry, 0.0),
            lowest_since_entry=_safe_float(lowest_since_entry, 0.0),
            holding_seconds=_safe_float(holding_seconds, 0.0),
        )
    except Exception:
        return
