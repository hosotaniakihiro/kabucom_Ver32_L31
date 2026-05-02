# ============================================================
# File   : trading/push/allocator/result_builder.py
# Version: Ver1.0-PRODUCTION-PUSH-ALLOCATOR-RESULT-BUILDER
# ------------------------------------------------------------
# ✔ final result builder
# ✔ push symbol diff
# ✔ state update
# ✔ safe output
# ✔ production safe
# ============================================================

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Set, List, Dict

from trading.push.allocator.state.state import AllocatorState

logger = logging.getLogger(__name__)


# ============================================================
# result structure
# ============================================================

@dataclass
class AllocationResult:
    """
    allocator最終結果
    """

    symbols: Set[str]
    added: Set[str]
    removed: Set[str]
    unchanged: Set[str]

    def to_list(self) -> List[str]:
        return sorted(self.symbols)

    def to_dict(self) -> Dict:
        return {
            "symbols": sorted(self.symbols),
            "added": sorted(self.added),
            "removed": sorted(self.removed),
            "unchanged": sorted(self.unchanged),
        }


# ============================================================
# result builder
# ============================================================

def build_result(
    final_symbols: Set[str],
    state: AllocatorState,
) -> AllocationResult:
    """
    allocator最終結果生成
    """

    current_symbols = state.current_symbols()

    final_symbols = set(final_symbols)

    added = final_symbols - current_symbols
    removed = current_symbols - final_symbols
    unchanged = final_symbols & current_symbols

    # --------------------------------------------------------
    # state update
    # --------------------------------------------------------

    state.update_symbols(final_symbols)

    logger.info(
        "[allocator result] "
        f"final={len(final_symbols)} "
        f"added={len(added)} "
        f"removed={len(removed)} "
        f"unchanged={len(unchanged)}"
    )

    if added:
        logger.info(f"[allocator add] {sorted(list(added))}")

    if removed:
        logger.info(f"[allocator remove] {sorted(list(removed))}")

    return AllocationResult(
        symbols=final_symbols,
        added=added,
        removed=removed,
        unchanged=unchanged,
    )