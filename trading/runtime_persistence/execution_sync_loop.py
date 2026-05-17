# ============================================================
# File   : trading/runtime_persistence/execution_sync_loop.py
# Version: Ver01-EXECUTION-SYNC-LOOP
# ------------------------------------------------------------
# 未約定/部分約定/全約定/取消を定期同期する汎用ループ。
#
# このモジュール自体は Kabu API の具体的な約定照会関数に依存しない。
# query_func を渡すことで、既存/今後追加の注文照会APIと接続できる。
#
# 目的:
#   - pending_orders_runtime を読み出す
#   - order_id ごとに query_func(order_id) で状態照会
#   - 約定があれば executions_runtime へ保存
#   - 取消/失敗/完了状態なら pending_orders_runtime を更新
#
# 注文は出さない。照会と保存のみ。
# ============================================================

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime
from typing import Any, Callable

from .runtime_state_store import load_pending_orders, mark_pending_order_done
from .execution_runtime_store import save_kabu_execution, save_kabu_executions
from .heartbeat_watchdog import heartbeat, mark_component_start, mark_component_stop

logger = logging.getLogger(__name__)

_STOP = False
_THREAD: threading.Thread | None = None


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


ENABLE_EXECUTION_SYNC_LOOP = _env_bool('ENABLE_EXECUTION_SYNC_LOOP', True)
EXECUTION_SYNC_INTERVAL_SEC = _env_float('EXECUTION_SYNC_INTERVAL_SEC', 5.0)

_DONE_STATUS_WORDS = {
    'DONE', 'FILLED', '約定済', '全約定', 'COMPLETE', 'COMPLETED', 'EXECUTED'
}
_CANCEL_STATUS_WORDS = {
    'CANCEL', 'CANCELED', 'CANCELLED', '取消', '取消済', 'EXPIRED'
}
_REJECT_STATUS_WORDS = {
    'REJECT', 'REJECTED', 'ERROR', 'FAILED', 'NG', '失敗', '拒否'
}
_PARTIAL_STATUS_WORDS = {
    'PARTIAL', 'PARTIALLY_FILLED', '一部約定', '部分約定'
}


def request_stop_execution_sync_loop() -> None:
    global _STOP
    _STOP = True


def _normalize_query_result(result: Any) -> dict:
    """
    query_funcの戻り値を標準形へ寄せる。

    受け入れる形式:
      - list[dict] 約定一覧
      - {'executions': [...], 'status': '...'}
      - {'orders': [...]} など
      - 単一dict
    """
    if result is None:
        return {'status': 'UNKNOWN', 'executions': [], 'raw': None}

    if isinstance(result, list):
        return {'status': 'FILLED' if result else 'UNKNOWN', 'executions': [x for x in result if isinstance(x, dict)], 'raw': result}

    if isinstance(result, dict):
        executions = result.get('executions') or result.get('Executions') or result.get('orders') or result.get('Orders') or []
        if isinstance(executions, dict):
            executions = [executions]
        if not isinstance(executions, list):
            executions = []

        status = (
            result.get('status')
            or result.get('Status')
            or result.get('state')
            or result.get('State')
            or result.get('OrderState')
            or 'UNKNOWN'
        )

        # 単一dict自体が約定っぽい場合
        if not executions and any(k in result for k in ('ExecutionID', 'ExecutionId', 'ExecutionPrice', 'ExecutionQty')):
            executions = [result]

        return {'status': str(status), 'executions': [x for x in executions if isinstance(x, dict)], 'raw': result}

    return {'status': 'UNKNOWN', 'executions': [], 'raw': result}


def _status_category(status: Any) -> str:
    s = str(status or '').upper()
    for w in _DONE_STATUS_WORDS:
        if str(w).upper() in s:
            return 'DONE'
    for w in _PARTIAL_STATUS_WORDS:
        if str(w).upper() in s:
            return 'PARTIAL'
    for w in _CANCEL_STATUS_WORDS:
        if str(w).upper() in s:
            return 'CANCELLED'
    for w in _REJECT_STATUS_WORDS:
        if str(w).upper() in s:
            return 'REJECTED'
    return 'PENDING'


def sync_pending_orders_once(query_func: Callable[[str], Any], *, trade_date: str | None = None) -> dict:
    """pending_orders_runtime を1回だけ照会・同期する。"""
    pending = load_pending_orders(trade_date=trade_date)
    checked = 0
    saved_exec = 0
    done = 0
    errors = 0

    for order in pending:
        order_id = str(order.get('order_id') or '')
        if not order_id:
            continue

        checked += 1
        try:
            raw = query_func(order_id)
            normalized = _normalize_query_result(raw)
            status = normalized.get('status')
            cat = _status_category(status)
            executions = normalized.get('executions') or []

            if executions:
                r = save_kabu_executions(executions, source='execution_sync_loop', trade_date=trade_date)
                saved_exec += int(r.get('saved') or 0)

            if cat in {'DONE', 'CANCELLED', 'REJECTED'}:
                mark_pending_order_done(order_id, status=cat, trade_date=trade_date)
                done += 1
            elif cat == 'PARTIAL':
                mark_pending_order_done(order_id, status='PARTIAL', trade_date=trade_date)

            logger.info(
                '[EXECUTION SYNC] order_id=%s status=%s cat=%s executions=%s',
                order_id,
                status,
                cat,
                len(executions),
            )

        except Exception:
            errors += 1
            logger.exception('[EXECUTION SYNC] failed order_id=%s', order_id)

    result = {
        'pending': len(pending),
        'checked': checked,
        'saved_executions': saved_exec,
        'done_or_closed': done,
        'errors': errors,
        'checked_at': datetime.now().isoformat(timespec='seconds'),
    }
    heartbeat('execution_sync_loop', status='OK' if errors == 0 else 'ERROR', detail=result)
    return result


def execution_sync_loop(query_func: Callable[[str], Any], *, interval_sec: float | None = None, trade_date: str | None = None) -> None:
    global _STOP
    if not ENABLE_EXECUTION_SYNC_LOOP:
        logger.warning('[EXECUTION SYNC] disabled by env')
        return

    sec = float(interval_sec if interval_sec is not None else EXECUTION_SYNC_INTERVAL_SEC)
    mark_component_start('execution_sync_loop', {'interval_sec': sec})
    logger.warning('[EXECUTION SYNC] loop start interval_sec=%s', sec)

    while not _STOP:
        try:
            result = sync_pending_orders_once(query_func, trade_date=trade_date)
            logger.info('[EXECUTION SYNC] result=%s', result)
        except Exception:
            heartbeat('execution_sync_loop', status='ERROR')
            logger.exception('[EXECUTION SYNC] loop failed')

        time.sleep(max(1.0, sec))

    mark_component_stop('execution_sync_loop')
    logger.warning('[EXECUTION SYNC] loop stopped')


def start_execution_sync_loop(query_func: Callable[[str], Any], *, interval_sec: float | None = None, trade_date: str | None = None) -> threading.Thread | None:
    """daemon threadとして約定同期loopを開始する。"""
    global _THREAD, _STOP
    if not ENABLE_EXECUTION_SYNC_LOOP:
        logger.warning('[EXECUTION SYNC] start skipped disabled')
        return None

    if _THREAD is not None and _THREAD.is_alive():
        logger.warning('[EXECUTION SYNC] already running')
        return _THREAD

    _STOP = False
    _THREAD = threading.Thread(
        target=execution_sync_loop,
        kwargs={'query_func': query_func, 'interval_sec': interval_sec, 'trade_date': trade_date},
        daemon=True,
        name='execution_sync_loop',
    )
    _THREAD.start()
    return _THREAD
