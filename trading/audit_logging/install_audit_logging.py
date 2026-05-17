# ============================================================
# File   : trading/audit_logging/install_audit_logging.py
# Version: Ver01-AUDIT-LOGGING-INSTALLER
# ------------------------------------------------------------
# バックテスト/監査ログ用パッチを起動時にまとめて有効化する。
# main.py / main_database.py / startup_orchestrator.py などから
# install_audit_logging() を1回呼ぶだけでよい。
# ============================================================

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_INSTALLED = False


def install_audit_logging() -> bool:
    """
    監査ログ基盤を起動時にまとめて有効化する。

    有効化対象:
      - audit DB schema 作成
      - entry_controller ENTRY_SKIP / AI_OK / ORDER結果 保存
      - buy_sell_entry _send_order 保存

    注意:
      - 約定監視・取消監視・exit_loop は、既存処理の場所が環境で異なるため、
        fill_cancel_audit.py / exit_loop_audit.py の関数を該当箇所から直接呼ぶ。
      - この関数自体は売買ロジックを変更しない。
    """
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
        from trading.audit_logging.entry_controller_audit_patch import install as install_entry_controller_patch
        if not install_entry_controller_patch():
            ok_all = False
    except Exception:
        logger.exception('[AUDIT INSTALL] entry_controller patch failed')
        ok_all = False

    try:
        from trading.audit_logging.buy_sell_entry_audit_patch import install as install_buy_sell_entry_patch
        if not install_buy_sell_entry_patch():
            ok_all = False
    except Exception:
        logger.exception('[AUDIT INSTALL] buy_sell_entry patch failed')
        ok_all = False

    _INSTALLED = True

    if ok_all:
        logger.warning('[AUDIT INSTALL] audit logging installed successfully')
    else:
        logger.warning('[AUDIT INSTALL] audit logging installed with some failures')

    return ok_all


# 互換名
install = install_audit_logging
