# ============================================================
# File   : core/startup/oneshot_limit_700k_patch.py
# Version: Ver02-ONESHOT-LIMIT-700K-AND-SUMMARY-AI-SYMBOL-RISK-PATCH
# ------------------------------------------------------------
# kabu_api.buy_sell_entry.MAX_ONESHOT を起動時に 700,000 円へ変更する。
# 併せて SUMMARY AI の daily risk 事前除外を銘柄単位に限定する。
# ============================================================

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_INSTALLED = False


def _install_summary_ai_symbol_risk_patch() -> bool:
    try:
        os.environ.setdefault("SUMMARY_AI_PRE_FILTER_DAILY_RISK", "1")
        os.environ.setdefault("SUMMARY_AI_PRE_FILTER_DAILY_RISK_SCOPE", "symbol_only")
        os.environ.setdefault("ENTRY_GLOBAL_MAX_DAILY_LOSS_YEN", "-50000")
        os.environ.setdefault("ENTRY_GLOBAL_MAX_CONSECUTIVE_LOSSES", "20")

        from core.startup import summary_ai_daily_risk_symbol_only_patch as p

        ok = p.install()
        logger.warning("[ONESHOT LIMIT PATCH] summary_ai_symbol_risk_patch installed=%s", ok)
        return bool(ok)
    except Exception:
        logger.exception("[ONESHOT LIMIT PATCH] summary_ai_symbol_risk_patch install failed")
        return False


def install() -> bool:
    global _INSTALLED

    if _INSTALLED:
        return True

    ok_main = False
    try:
        from kabu_api import buy_sell_entry as bse

        old_value = getattr(bse, "MAX_ONESHOT", None)
        bse.MAX_ONESHOT = 700_000
        ok_main = True

        logger.warning(
            "[ONESHOT LIMIT PATCH] MAX_ONESHOT changed old=%s new=%s",
            old_value,
            bse.MAX_ONESHOT,
        )
    except Exception:
        logger.exception("[ONESHOT LIMIT PATCH] install failed")

    ok_symbol_risk = _install_summary_ai_symbol_risk_patch()

    _INSTALLED = bool(ok_main or ok_symbol_risk)
    return _INSTALLED
