# ============================================================
# File   : core/startup/entry_liquidity_runtime_patch.py
# Version: V2-FULLY-INLINED
# ------------------------------------------------------------
# V2: 直近summary DBを使った流動性ガード (出来高/売買代金/値動き、
#     turnover の円単位正規化つき) は
#     trading/handlers/entry_controller.py の _recent_liq_* 群 (Ver2.9) へ
#     インライン化済みのため撤去した。
#     summary_ai_liquidity_runtime_patch の companion install のみ、ここに残す。
# ============================================================

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)
_INSTALLED = False


def _install_summary_ai_liquidity_guard() -> bool:
    try:
        from core.startup.summary_ai_liquidity_runtime_patch import install as install_summary_ai_liq
        ok = install_summary_ai_liq()
        logger.warning("[ENTRY LIQ GUARD] summary_ai_liquidity_runtime_patch installed=%s", ok)
        return bool(ok)
    except Exception:
        logger.exception("[ENTRY LIQ GUARD] summary_ai_liquidity_runtime_patch install failed")
        return False


def install() -> bool:
    global _INSTALLED
    ok = _install_summary_ai_liquidity_guard()
    _INSTALLED = True
    return bool(ok)


try:
    install()
except Exception:
    logger.exception("[ENTRY LIQ GUARD] auto install failed")

__all__ = ["install"]
