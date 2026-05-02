# ============================================================
# File   : trading/entry/pipeline/guards.py
# Function:
#   - ETF / ETN / REIT / FUND 除外
#   - tonosama guard
#   - position guard
#   - market regime guard
# ------------------------------------------------------------
# Version: Ver39-PRODUCTION-ENTRY-PIPELINE-GUARDS
# ============================================================

from __future__ import annotations

import logging
from typing import Tuple

from .imports import (
    get_tradeable_symbols,
    allow_tonosama_entry,
    can_entry_symbol,
    detect_market_regime,
    allow_entry,
)

from .utils import (
    safe_symbol,
)

logger = logging.getLogger(__name__)


def is_etf(symbol: str) -> bool:
    """
    get_tradeable_symbols に入っていない銘柄を ETF/ETN/REIT/FUND 扱いで除外。

    注意:
      - get_tradeable_symbols() が None の場合は除外しない。
      - 例外時も安全側に倒しすぎると全停止するため False。
    """

    try:
        symbol = safe_symbol(symbol)

        if not symbol:
            return True

        if get_tradeable_symbols is None:
            return False

        tradeable = get_tradeable_symbols()

        if tradeable is None:
            return False

        if isinstance(tradeable, (list, tuple, set)):
            tradeable_set = {safe_symbol(x) for x in tradeable if safe_symbol(x)}
            return symbol not in tradeable_set

        return False

    except Exception:
        logger.exception("[ETF GUARD] failed symbol=%s", symbol)
        return False


def pass_symbol_guards(
    *,
    symbol: str,
    side: str,
    source: str,
    log_prefix: str,
) -> Tuple[bool, str]:
    """
    symbol 単位の共通 entry guard。

    Returns:
      (ok, reason)
    """

    try:
        symbol = safe_symbol(symbol)

        if not symbol:
            return False, "empty_symbol"

        if is_etf(symbol):
            logger.debug("%s ETF skipped symbol=%s", log_prefix, symbol)
            return False, "etf_or_not_tradeable"

        if not allow_tonosama_entry(symbol):
            logger.debug("%s tonosama skipped symbol=%s", log_prefix, symbol)
            return False, "tonosama_blocked"

        ok, reason_ng = can_entry_symbol(
            symbol,
            side,
            source=source,
            with_reason=True,
        )

        if not ok:
            logger.debug(
                "%s position blocked symbol=%s side=%s source=%s reason=%s",
                log_prefix,
                symbol,
                side,
                source,
                reason_ng,
            )
            return False, str(reason_ng)

        return True, ""

    except Exception:
        logger.exception("%s symbol guard failed symbol=%s side=%s", log_prefix, symbol, side)
        return False, "guard_exception"


def pass_market_regime_guard(*, log_prefix: str) -> bool:
    """
    market regime guard。
    """

    try:
        detect_market_regime()

        if not allow_entry():
            logger.info("%s market regime blocked entry", log_prefix)
            return False

        return True

    except Exception:
        logger.exception("%s market regime guard failed", log_prefix)
        return False