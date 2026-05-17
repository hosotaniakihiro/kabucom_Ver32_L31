# ============================================================
# File   : trading/audit_logging/exit_loop_audit.py
# Version: Ver01-EXIT-LOOP-AUDIT
# ------------------------------------------------------------
# exit_loop_5s / exit_manager から呼び出す監査ログ部品。
# 5秒ごとの状態、0.3%逆行、トレーリング、5分停滞をDBへ保存する。
# ============================================================

from __future__ import annotations

from datetime import datetime
from typing import Any

from trading.audit_logging.order_exit_audit import (
    audit_exit_check,
    audit_position_snapshot,
    audit_order_filled,
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


def calc_exit_metrics(side: str, entry_price: Any, current_price: Any, highest_since_entry: Any = None, lowest_since_entry: Any = None) -> dict:
    """exit判定の基礎メトリクスを計算する。"""
    side_u = str(side or '').upper()
    entry = _safe_float(entry_price, 0.0)
    current = _safe_float(current_price, 0.0)
    high = _safe_float(highest_since_entry, current)
    low = _safe_float(lowest_since_entry, current)

    if high <= 0:
        high = current
    if low <= 0:
        low = current

    adverse_pct = 0.0
    trail_pct = 0.0

    if entry > 0 and current > 0:
        if side_u == 'BUY':
            adverse_pct = (entry - current) / entry * 100.0
        elif side_u == 'SELL':
            adverse_pct = (current - entry) / entry * 100.0

    if current > 0:
        if side_u == 'BUY' and high > 0:
            trail_pct = (high - current) / high * 100.0
        elif side_u == 'SELL' and low > 0:
            trail_pct = (current - low) / low * 100.0

    return {
        'side': side_u,
        'entry_price': entry,
        'current_price': current,
        'highest_since_entry': high,
        'lowest_since_entry': low,
        'adverse_pct': adverse_pct,
        'trail_pct': trail_pct,
    }


def audit_exit_loop_snapshot(symbol: str, side: str, qty: Any, entry_price: Any, current_price: Any, highest_since_entry: Any = None, lowest_since_entry: Any = None, holding_seconds: Any = None, exit_reason: str = '', triggered: bool = False) -> None:
    """
    exit_loop_5s で毎回呼ぶ想定。
    position_state_history と exit_history の両方へ保存する。
    """
    try:
        m = calc_exit_metrics(side, entry_price, current_price, highest_since_entry, lowest_since_entry)

        audit_position_snapshot(
            symbol=symbol,
            side=side,
            qty=_safe_int(qty, 0),
            entry_price=m['entry_price'],
            highest_since_entry=m['highest_since_entry'],
            lowest_since_entry=m['lowest_since_entry'],
            holding_seconds=_safe_float(holding_seconds, 0.0),
        )

        reason = exit_reason or 'EXIT_LOOP_CHECK'
        if not triggered:
            reason = (
                f"{reason}|adverse_pct={m['adverse_pct']:.4f}"
                f"|trail_pct={m['trail_pct']:.4f}"
                f"|holding_seconds={_safe_float(holding_seconds, 0.0):.1f}"
            )

        audit_exit_check(
            symbol=symbol,
            side=side,
            entry_price=m['entry_price'],
            current_price=m['current_price'],
            highest_since_entry=m['highest_since_entry'],
            lowest_since_entry=m['lowest_since_entry'],
            exit_reason=reason,
            triggered=triggered,
        )
    except Exception:
        return


def audit_exit_trigger(symbol: str, side: str, qty: Any, entry_price: Any, current_price: Any, highest_since_entry: Any = None, lowest_since_entry: Any = None, exit_reason: str = '', holding_seconds: Any = None) -> None:
    """exit発火時に呼ぶ。"""
    try:
        audit_exit_loop_snapshot(
            symbol=symbol,
            side=side,
            qty=qty,
            entry_price=entry_price,
            current_price=current_price,
            highest_since_entry=highest_since_entry,
            lowest_since_entry=lowest_since_entry,
            holding_seconds=holding_seconds,
            exit_reason=exit_reason or 'EXIT_TRIGGERED',
            triggered=True,
        )
    except Exception:
        return


def audit_exit_filled(symbol: str, side: str, qty: Any, order_id: Any = '', exit_price: Any = None, exit_reason: str = '') -> None:
    """exit注文が約定した時に呼ぶ。"""
    try:
        audit_order_filled(
            symbol=symbol,
            side=side,
            qty=qty,
            order_type='EXIT_MARKET',
            order_id=order_id,
            price=exit_price,
            filled_price=exit_price,
            detail={
                'source': 'exit_loop_audit',
                'exit_reason': exit_reason,
                'audit_time': datetime.now().isoformat(timespec='seconds'),
            },
        )
    except Exception:
        return
