# ============================================================
# File   : trading/runtime_persistence/crash_recovery_supervisor.py
# Version: Ver01-CRASH-RECOVERY-SUPERVISOR
# ------------------------------------------------------------
# 日中停止・再起動後の復旧監督モジュール。
#
# 目的:
#   - runtime_state.db の OPEN positions / pending orders を読み出す
#   - kabu API 実建玉リストが渡された場合は reconcile する
#   - exit_loop 開始前に、復元状態をログとdictで確認できるようにする
#   - 注文は出さない。DB復元/診断のみ。
# ============================================================

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Any, Callable

from .runtime_state_store import (
    ensure_runtime_state_db,
    load_open_positions,
    load_pending_orders,
    load_latest_portfolio_state,
)
from .kabu_reconciliation import reconcile_kabu_positions

logger = logging.getLogger(__name__)

BASE_DIR = r'\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\runtime_state'


def _today() -> str:
    return datetime.now().strftime('%Y%m%d')


def _out_path(trade_date: str | None = None) -> str:
    td = trade_date or _today()
    os.makedirs(BASE_DIR, exist_ok=True)
    return os.path.join(BASE_DIR, f'crash_recovery_{td}.json')


def _safe_call(fn: Callable[[], Any], label: str) -> tuple[bool, Any]:
    try:
        return True, fn()
    except Exception as e:
        logger.exception('[CRASH RECOVERY] %s failed', label)
        return False, {'error': str(e), 'label': label}


class CrashRecoverySupervisor:
    def __init__(self, trade_date: str | None = None):
        self.trade_date = trade_date or _today()

    def restore_from_runtime_db(self) -> dict:
        ensure_runtime_state_db(self.trade_date)

        ok_pos, positions = _safe_call(lambda: load_open_positions(self.trade_date), 'load_open_positions')
        ok_ord, pending_orders = _safe_call(lambda: load_pending_orders(self.trade_date), 'load_pending_orders')
        ok_pf, portfolio = _safe_call(lambda: load_latest_portfolio_state(self.trade_date), 'load_latest_portfolio_state')

        result = {
            'trade_date': self.trade_date,
            'runtime_restore_ok': bool(ok_pos and ok_ord and ok_pf),
            'open_positions_count': len(positions) if isinstance(positions, list) else 0,
            'pending_orders_count': len(pending_orders) if isinstance(pending_orders, list) else 0,
            'open_positions': positions if isinstance(positions, list) else [],
            'pending_orders': pending_orders if isinstance(pending_orders, list) else [],
            'portfolio': portfolio if isinstance(portfolio, dict) else {},
            'restored_at': datetime.now().isoformat(timespec='seconds'),
        }

        logger.warning('[CRASH RECOVERY] runtime restore result=%s', result)
        return result

    def reconcile_with_kabu(self, kabu_positions: list[dict] | None = None) -> dict:
        if kabu_positions is None:
            return {
                'trade_date': self.trade_date,
                'kabu_reconcile_ok': False,
                'reason': 'kabu_positions not supplied',
            }
        result = reconcile_kabu_positions(kabu_positions, trade_date=self.trade_date)
        result['kabu_reconcile_ok'] = True
        return result

    def run(self, kabu_positions: list[dict] | None = None, save_json: bool = True) -> dict:
        runtime = self.restore_from_runtime_db()
        kabu = self.reconcile_with_kabu(kabu_positions)

        # reconcile 後にもう一度 runtime 状態を読む。
        after = self.restore_from_runtime_db()

        result = {
            'trade_date': self.trade_date,
            'runtime_before': runtime,
            'kabu_reconcile': kabu,
            'runtime_after': after,
            'recovery_ok': bool(after.get('runtime_restore_ok')),
            'recovered_at': datetime.now().isoformat(timespec='seconds'),
            'note': '注文は出していません。runtime_state復元とkabu建玉同期診断のみです。',
        }

        if save_json:
            path = _out_path(self.trade_date)
            result['json_path'] = path
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(result, f, ensure_ascii=False, indent=2, default=str)

        logger.warning('[CRASH RECOVERY] supervisor result=%s', result)
        return result


def run_crash_recovery(trade_date: str | None = None, kabu_positions: list[dict] | None = None) -> dict:
    return CrashRecoverySupervisor(trade_date=trade_date).run(kabu_positions=kabu_positions)
