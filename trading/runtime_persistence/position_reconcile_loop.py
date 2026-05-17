# ============================================================
# File   : trading/runtime_persistence/position_reconcile_loop.py
# Version: Ver01-POSITION-RECONCILE-LOOP
# ------------------------------------------------------------
# Kabu実建玉と runtime_state.db を定期的に突き合わせる。
#
# 目的:
#   - runtime_state の OPEN positions と Kabu実建玉のズレを補正
#   - 再起動後・約定同期漏れ・手動決済後の状態ズレを減らす
#   - highest_since_entry / lowest_since_entry を維持しながら同期
#
# このモジュールは注文を出さない。照会とDB同期のみ。
# ============================================================

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime
from typing import Any, Callable

from .kabu_reconciliation import reconcile_kabu_positions
from .runtime_state_store import load_open_positions
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


ENABLE_POSITION_RECONCILE_LOOP = _env_bool('ENABLE_POSITION_RECONCILE_LOOP', True)
POSITION_RECONCILE_INTERVAL_SEC = _env_float('POSITION_RECONCILE_INTERVAL_SEC', 60.0)


def request_stop_position_reconcile_loop() -> None:
    global _STOP
    _STOP = True


def _normalize_positions_result(result: Any) -> list[dict]:
    """
    get_positions_func の戻り値を list[dict] に寄せる。

    受け入れる形式:
      - list[dict]
      - {'positions': [...]}
      - {'Positions': [...]}
      - {'data': [...]}
      - 単一dict
    """
    if result is None:
        return []

    if isinstance(result, list):
        return [x for x in result if isinstance(x, dict)]

    if isinstance(result, dict):
        for key in ('positions', 'Positions', 'data', 'Data', 'items', 'Items'):
            v = result.get(key)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
            if isinstance(v, dict):
                return [v]

        # 単一建玉dictっぽい場合
        if any(k in result for k in ('Symbol', 'symbol', 'Code', 'code')):
            return [result]

    return []


def reconcile_positions_once(get_positions_func: Callable[[], Any], *, trade_date: str | None = None) -> dict:
    """Kabu実建玉取得関数を1回呼び、runtime_state と同期する。"""
    try:
        raw = get_positions_func()
        positions = _normalize_positions_result(raw)

        before = load_open_positions(trade_date=trade_date)
        result = reconcile_kabu_positions(positions, trade_date=trade_date)
        after = load_open_positions(trade_date=trade_date)

        out = {
            'trade_date': trade_date,
            'raw_type': type(raw).__name__,
            'kabu_positions': len(positions),
            'runtime_open_before': len(before),
            'runtime_open_after': len(after),
            'reconcile': result,
            'checked_at': datetime.now().isoformat(timespec='seconds'),
        }
        heartbeat('position_reconcile_loop', status='OK', detail=out)
        logger.warning('[POSITION RECONCILE] once result=%s', out)
        return out

    except Exception as e:
        out = {
            'trade_date': trade_date,
            'error': str(e),
            'checked_at': datetime.now().isoformat(timespec='seconds'),
        }
        heartbeat('position_reconcile_loop', status='ERROR', detail=out)
        logger.exception('[POSITION RECONCILE] once failed')
        return out


def position_reconcile_loop(get_positions_func: Callable[[], Any], *, interval_sec: float | None = None, trade_date: str | None = None) -> None:
    global _STOP
    if not ENABLE_POSITION_RECONCILE_LOOP:
        logger.warning('[POSITION RECONCILE] disabled by env')
        return

    sec = float(interval_sec if interval_sec is not None else POSITION_RECONCILE_INTERVAL_SEC)
    mark_component_start('position_reconcile_loop', {'interval_sec': sec})
    logger.warning('[POSITION RECONCILE] loop start interval_sec=%s', sec)

    while not _STOP:
        try:
            reconcile_positions_once(get_positions_func, trade_date=trade_date)
        except Exception:
            heartbeat('position_reconcile_loop', status='ERROR')
            logger.exception('[POSITION RECONCILE] loop failed')

        time.sleep(max(5.0, sec))

    mark_component_stop('position_reconcile_loop')
    logger.warning('[POSITION RECONCILE] loop stopped')


def start_position_reconcile_loop(get_positions_func: Callable[[], Any], *, interval_sec: float | None = None, trade_date: str | None = None) -> threading.Thread | None:
    """daemon threadとして実建玉reconcile loopを開始する。"""
    global _THREAD, _STOP
    if not ENABLE_POSITION_RECONCILE_LOOP:
        logger.warning('[POSITION RECONCILE] start skipped disabled')
        return None

    if _THREAD is not None and _THREAD.is_alive():
        logger.warning('[POSITION RECONCILE] already running')
        return _THREAD

    _STOP = False
    _THREAD = threading.Thread(
        target=position_reconcile_loop,
        kwargs={'get_positions_func': get_positions_func, 'interval_sec': interval_sec, 'trade_date': trade_date},
        daemon=True,
        name='position_reconcile_loop',
    )
    _THREAD.start()
    return _THREAD
