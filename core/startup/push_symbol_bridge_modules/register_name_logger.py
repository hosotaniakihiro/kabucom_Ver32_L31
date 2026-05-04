# ============================================================
# File   : core/startup/push_symbol_bridge_modules/register_name_logger.py
# Version: PRODUCTION-STABLE-REV3.0
# ------------------------------------------------------------
# Purpose:
#   登録対象50銘柄を 銘柄コード(銘柄名) 形式で1行表示する。
# ============================================================

from __future__ import annotations

import logging
from typing import Sequence

from .constants import DEFAULT_REGISTER_LIMIT
from .normalize import clean_symbols

logger = logging.getLogger(__name__)


def log_register_symbols_with_names(
    register_symbols: Sequence[str],
    *,
    reason: str = "startup_bridge",
) -> None:
    """
    登録対象50銘柄を 銘柄コード(銘柄名) 形式で1行表示する。

    優先:
      1. trading.push.subscription_manager.register_symbol_logger
      2. fallback: 銘柄コードのみ1行表示
    """
    try:
        try:
            from trading.push.subscription_manager.register_symbol_logger import (
                log_kabustation_register_symbols,
            )

            log_kabustation_register_symbols(
                register_symbols,
                reason=reason,
                one_line=True,
                show_current=False,
                show_diff=False,
                force_reload_symbol_names=False,
            )
            return

        except Exception:
            logger.debug(
                "[PUSH SYMBOL BRIDGE] register_symbol_logger import/call failed -> fallback",
                exc_info=True,
            )

        cleaned = clean_symbols(register_symbols, limit=DEFAULT_REGISTER_LIMIT)
        line = ", ".join(cleaned)

        logger.info(
            "[PUSH SYMBOL BRIDGE REGISTER TARGETS LINE] reason=%s count=%d symbols=%s",
            reason,
            len(cleaned),
            line,
        )

    except Exception:
        logger.exception(
            "[PUSH SYMBOL BRIDGE] register symbols one-line log failed reason=%s",
            reason,
        )


__all__ = [
    "log_register_symbols_with_names",
]
