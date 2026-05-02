# ============================================================
# File   : trading/push/allocator/governor.py
# Version: Ver1.0-PRODUCTION-PUSH-ALLOCATOR-GOVERNOR
# ------------------------------------------------------------
# ✔ push allocation governance layer
# ✔ max symbol guard
# ✔ duplicate guard
# ✔ symbol normalization
# ✔ final churn guard
# ✔ runtime safety
# ✔ production safe
# ============================================================

from __future__ import annotations

import logging
from typing import Iterable, Set

from trading.push.allocator.state.state import AllocatorState

logger = logging.getLogger(__name__)


# ============================================================
# normalize symbol
# ============================================================

def normalize_symbol(symbol: str | None) -> str | None:

    if symbol is None:
        return None

    try:
        s = str(symbol).strip()
    except Exception:
        return None

    if not s:
        return None

    return s


# ============================================================
# normalize symbol set
# ============================================================

def normalize_symbols(symbols: Iterable[str]) -> Set[str]:

    out: Set[str] = set()

    for s in symbols:

        s = normalize_symbol(s)

        if s is None:
            continue

        out.add(s)

    return out


# ============================================================
# apply governance
# ============================================================

def apply_governor(
    selected_symbols: Iterable[str],
    config,
    state: AllocatorState,
) -> Set[str]:
    """
    最終 push symbol 制御
    """

    # --------------------------------------------------------
    # normalize
    # --------------------------------------------------------

    symbols = normalize_symbols(selected_symbols)

    # --------------------------------------------------------
    # empty guard
    # --------------------------------------------------------

    if not symbols:

        logger.warning("[allocator governor] empty selection")

        return state.current_symbols()

    # --------------------------------------------------------
    # max symbol guard
    # --------------------------------------------------------

    max_symbols = getattr(config, "max_push_symbols", 50)

    if len(symbols) > max_symbols:

        logger.warning(
            "[allocator governor] limit exceeded "
            f"{len(symbols)} > {max_symbols}"
        )

        symbols = set(list(symbols)[:max_symbols])

    # --------------------------------------------------------
    # churn protection (extra safety)
    # --------------------------------------------------------

    current = state.current_symbols()

    for sym in current:

        if sym in symbols:
            continue

        if not state.can_remove(sym, config.min_hold_seconds):
            symbols.add(sym)

    # --------------------------------------------------------
    # re-limit
    # --------------------------------------------------------

    if len(symbols) > max_symbols:

        symbols = set(list(symbols)[:max_symbols])

    logger.info(
        "[allocator governor] "
        f"final_symbols={len(symbols)}"
    )

    return symbols