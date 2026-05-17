# ============================================================
# File   : core/startup/oneshot_limit_700k_patch.py
# Version: Ver01-ONESHOT-LIMIT-700K-PATCH
# ------------------------------------------------------------
# kabu_api.buy_sell_entry.MAX_ONESHOT を起動時に 700,000 円へ変更する。
# buy_sell_entry.py 本体を壊さず、runtime patch として適用する。
# ============================================================

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_INSTALLED = False


def install() -> bool:
    global _INSTALLED

    if _INSTALLED:
        return True

    try:
        from kabu_api import buy_sell_entry as bse

        old_value = getattr(bse, 'MAX_ONESHOT', None)
        bse.MAX_ONESHOT = 700_000
        _INSTALLED = True

        logger.warning(
            '[ONESHOT LIMIT PATCH] MAX_ONESHOT changed old=%s new=%s',
            old_value,
            bse.MAX_ONESHOT,
        )
        return True

    except Exception:
        logger.exception('[ONESHOT LIMIT PATCH] install failed')
        return False
