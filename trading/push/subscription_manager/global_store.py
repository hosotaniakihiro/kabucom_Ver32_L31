# ============================================================
# File   : trading/push/subscription_manager/global_store.py
# Version: V1.0-PUSH-SUBSCRIPTION-GLOBAL-STORE
# ------------------------------------------------------------
# Purpose:
#   - subscription manager が作成した候補リストを global_data へ保存する。
# ============================================================

from __future__ import annotations

import logging
from typing import Optional, Sequence

from .globals_access import safe_get_global_data, safe_setattr

logger = logging.getLogger(__name__)


def save_symbol_lists_to_global_data(
    raw_symbols: Optional[Sequence[str]] = None,
    buy_symbols: Optional[Sequence[str]] = None,
    sell_symbols: Optional[Sequence[str]] = None,
    filtered_symbols: Optional[Sequence[str]] = None,
    ranking_symbols: Optional[Sequence[str]] = None,
    rotation_a_symbols: Optional[Sequence[str]] = None,
    rotation_b_symbols: Optional[Sequence[str]] = None,
    priority_symbols: Optional[Sequence[str]] = None,
    position_symbols: Optional[Sequence[str]] = None,
) -> None:
    gd = safe_get_global_data()
    if gd is None:
        return

    try:
        if raw_symbols is not None:
            safe_setattr(gd, "raw_candidate_symbols", list(raw_symbols))
        if buy_symbols is not None:
            safe_setattr(gd, "buy_candidate_symbols", list(buy_symbols))
        if sell_symbols is not None:
            safe_setattr(gd, "sell_candidate_symbols", list(sell_symbols))
        if ranking_symbols is not None:
            safe_setattr(gd, "ranking_candidate_symbols", list(ranking_symbols))
        if rotation_a_symbols is not None:
            safe_setattr(gd, "rotation_a_symbols", list(rotation_a_symbols))
        if rotation_b_symbols is not None:
            safe_setattr(gd, "rotation_b_symbols", list(rotation_b_symbols))
        if priority_symbols is not None:
            safe_setattr(gd, "protected_push_symbols", list(priority_symbols))
            safe_setattr(gd, "priority_push_symbols", list(priority_symbols))
        if position_symbols is not None:
            safe_setattr(gd, "position_push_symbols", list(position_symbols))

        if filtered_symbols is not None:
            safe_setattr(gd, "filtered_candidate_symbols", list(filtered_symbols))
            safe_setattr(gd, "push_symbols", list(filtered_symbols))
            safe_setattr(gd, "subscription_targets", list(filtered_symbols))
            safe_setattr(gd, "ats_register_targets", list(filtered_symbols))
            safe_setattr(gd, "ats_targets", list(filtered_symbols))
            safe_setattr(gd, "should_register_symbols", list(filtered_symbols))

    except Exception:
        logger.exception("[SUB MANAGER GLOBAL STORE] failed to save symbol lists")


__all__ = [
    "save_symbol_lists_to_global_data",
]
