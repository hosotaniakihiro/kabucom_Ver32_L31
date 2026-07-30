# ============================================================
# File   : trading/audit_logging/entry_controller_audit_patch.py
# Version: Ver02-ENTRY-CONTROLLER-AUDIT-FULL-LINKS
# ------------------------------------------------------------
# entry_controller に監査ログを後付けするための安全パッチ。
# 売買判定ロジックは変更せず、DB保存だけを追加する。
# ============================================================

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_INSTALLED = False
_ORIGINAL_EXECUTE_BEST_CANDIDATE = None

# ENTRY_SKIP の監査ログ記録 (旧 patched_log_skip) は
# trading/handlers/entry_controller.py の _log_skip 本体 (Ver2.7) へ
# audit_entry_skip 呼び出しとしてインライン化済み。


def install() -> bool:
    """
    trading.handlers.entry_controller へ監査ログを差し込む。

    保存対象:
      - ENTRY_SKIP -> filter_history
      - AI_GATE_OK済み発注候補 -> candidate_history
      - ENTRY_DISPATCH / ORDER_ACCEPTED / FAILED -> order_history

    監査ログ側で例外が出ても、発注処理は止めない。
    """
    global _INSTALLED, _ORIGINAL_EXECUTE_BEST_CANDIDATE

    if _INSTALLED:
        return True

    try:
        from trading.handlers import entry_controller as ec
        from trading.audit_logging.entry_audit import (
            audit_candidate_ok,
            audit_order,
            _build_entry_id,
        )
    except Exception:
        logger.exception('[AUDIT PATCH] import failed')
        return False

    if getattr(ec, '_AUDIT_LOGGING_PATCH_INSTALLED', False):
        _INSTALLED = True
        return True

    _ORIGINAL_EXECUTE_BEST_CANDIDATE = ec._execute_best_candidate

    def patched_execute_best_candidate(item: dict, boost_active: bool) -> bool:
        entry_id = ''
        try:
            symbol = item.get('symbol')
            side = item.get('side')
            entry_row = item.get('entry_row') or {}
            ai = item.get('ai') or {}
            ai_msg = item.get('ai_msg') or ''
            entry_id = _build_entry_id(symbol, side, entry_row)
            audit_candidate_ok(symbol=symbol, side=side, entry_row=entry_row, ai=ai, ai_msg=ai_msg)
        except Exception:
            pass

        # 元関数の中で ENTRY_DISPATCH / ORDER_ID_EMPTY / ENTRY_APPROVED がログ出力される。
        # ここでは呼び出し前後で最低限の注文イベントを残す。
        try:
            symbol = item.get('symbol')
            side = item.get('side')
            entry_row = item.get('entry_row') or {}
            ai = item.get('ai') or {}
            ai_msg = item.get('ai_msg') or ''
            price = ec._resolve_price(entry_row)
            qty = ec.calculate_entry_quantity(
                symbol=symbol,
                price=price,
                confidence=ai.get('confidence', 0.0),
                lot_multiplier=ai.get('lot_multiplier', 1.0),
                atr=entry_row.get('atr') or entry_row.get('atr_1m') or entry_row.get('atr_5m'),
            )
            audit_order(
                symbol=symbol,
                side=side,
                qty=qty,
                order_type='PRE_DISPATCH',
                price=price,
                status='CANDIDATE_SELECTED',
                reason='before_execute_best_candidate',
                entry_row=entry_row,
                ai=ai,
                ai_msg=ai_msg,
                entry_id=entry_id,
            )
        except Exception:
            pass

        ok = _ORIGINAL_EXECUTE_BEST_CANDIDATE(item, boost_active=boost_active)

        try:
            symbol = item.get('symbol')
            side = item.get('side')
            entry_row = item.get('entry_row') or {}
            ai = item.get('ai') or {}
            ai_msg = item.get('ai_msg') or ''
            price = ec._resolve_price(entry_row)
            audit_order(
                symbol=symbol,
                side=side,
                qty=0,
                order_type='ENTRY_PIPELINE',
                price=price,
                status='ORDER_ACCEPTED' if ok else 'FAILED_OR_SKIPPED',
                reason='execute_best_candidate_result',
                entry_row=entry_row,
                ai=ai,
                ai_msg=ai_msg,
                entry_id=entry_id,
            )
        except Exception:
            pass

        return ok

    ec._execute_best_candidate = patched_execute_best_candidate
    ec._AUDIT_LOGGING_PATCH_INSTALLED = True
    _INSTALLED = True

    logger.warning('[AUDIT PATCH] entry_controller audit logging installed full_links=True')
    return True
