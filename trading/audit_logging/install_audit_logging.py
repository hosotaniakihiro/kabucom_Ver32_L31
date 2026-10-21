# ============================================================
# File   : trading/audit_logging/install_audit_logging.py
# Version: Ver03-AUDIT-LOGGING-INSTALLER-FULL-TRADE-LINKS-COMPACT
# ============================================================
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)
_INSTALLED = False


def install_audit_logging() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    ok_all = True

    try:
        from trading.audit_logging.recorder import ensure_audit_db
        ensure_audit_db()
        logger.warning('[AUDIT INSTALL] audit db ensured')
    except Exception:
        logger.exception('[AUDIT INSTALL] ensure_audit_db failed')
        ok_all = False

    try:
        # native of core/startup/position_trade_reason_schema_patch.install()
        from trading.audit_logging.recorder import ensure_position_trade_reason_schema
        ensure_position_trade_reason_schema()
    except Exception:
        logger.exception('[AUDIT INSTALL] position reason schema patch failed')
        ok_all = False

    # audit_full_trade_link_patch / audit_full_trade_link_no_duplicate_patch:
    # native化済み。recorder.record_*/ensure_audit_db が最終合成後の挙動
    # (entry_id/reason_code/ranking_snapshot等の拡張列へ1件だけ書き込む) を
    # 直接実装しているため、ここでの動的パッチ適用は不要。

    # entry_controller_audit_patch は削除した。
    #   candidate_history / order_history -> trading/handlers/entry_controller.py の
    #     _audit_candidate_ok_safe / _audit_order_safe (Ver2.9)
    #   ENTRY_SKIP -> 同ファイルの _log_skip 本体 (Ver2.7)
    # へインライン化済みで、install() が True を返すだけの空スタブになっていた。

    # buy_sell_entry_audit_patch は kabu_api/buy_sell_entry.py の _send_order 本体へ
    # 本文化した (_audit_* ヘルパ + ネイティブを _send_order_core へ改名)。
    # 同関数を包む相手は他に無く、呼び出し元 6 箇所も同ファイル内のモジュール名解決
    # なので、install なしで従来と同じ範囲に効く。

    _INSTALLED = True
    logger.warning('[AUDIT INSTALL] audit logging installed full_trade_links=True compact_writer=True ok=%s', ok_all)
    return ok_all


install = install_audit_logging
