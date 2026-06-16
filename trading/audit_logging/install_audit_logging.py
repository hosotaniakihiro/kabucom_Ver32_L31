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
        from core.startup.position_trade_reason_schema_patch import install as install_position_reason_schema
        ok_all = bool(install_position_reason_schema()) and ok_all
    except Exception:
        logger.exception('[AUDIT INSTALL] position reason schema patch failed')
        ok_all = False

    try:
        from core.startup.audit_full_trade_link_patch import install as install_audit_full_link
        ok_all = bool(install_audit_full_link()) and ok_all
    except Exception:
        logger.exception('[AUDIT INSTALL] audit full trade link patch failed')
        ok_all = False

    try:
        from core.startup.audit_full_trade_link_no_duplicate_patch import install as install_audit_compact_writer
        ok_all = bool(install_audit_compact_writer()) and ok_all
    except Exception:
        logger.exception('[AUDIT INSTALL] audit compact writer patch failed')
        ok_all = False

    try:
        from trading.audit_logging.entry_controller_audit_patch import install as install_entry_controller_patch
        ok_all = bool(install_entry_controller_patch()) and ok_all
    except Exception:
        logger.exception('[AUDIT INSTALL] entry_controller patch failed')
        ok_all = False

    try:
        from trading.audit_logging.buy_sell_entry_audit_patch import install as install_buy_sell_entry_patch
        ok_all = bool(install_buy_sell_entry_patch()) and ok_all
    except Exception:
        logger.exception('[AUDIT INSTALL] buy_sell_entry patch failed')
        ok_all = False

    _INSTALLED = True
    logger.warning('[AUDIT INSTALL] audit logging installed full_trade_links=True compact_writer=True ok=%s', ok_all)
    return ok_all


install = install_audit_logging
