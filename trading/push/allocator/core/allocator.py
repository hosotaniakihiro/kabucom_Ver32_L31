# ============================================================
# File   : trading/push/allocator/allocator.py
# Version: Ver1.0-PRODUCTION-PUSH-SLOT-ALLOCATOR
# ------------------------------------------------------------
# ✔ push slot allocator orchestrator
# ✔ pipeline integration
# ✔ runtime safe
# ✔ production ready
# ============================================================

from __future__ import annotations

import logging
from typing import Any

from trading.push.allocator.config import DEFAULT_ALLOCATOR_CONFIG
from trading.push.allocator.state.state import AllocatorState, allocator_state

from trading.push.allocator.core.candidate_preparer import prepare_candidates
from trading.push.allocator.core.annotator import annotate_candidates
from trading.push.allocator.scoring.scoring import apply_scoring
from trading.push.allocator.core.selector import select_symbols
from trading.push.allocator.governance.governor import apply_governor
from trading.push.allocator.core.result_builder import build_result, AllocationResult

logger = logging.getLogger(__name__)


# ============================================================
# allocator class
# ============================================================

class PushSlotAllocator:
    """
    push slot allocator
    """

    def __init__(
        self,
        config=None,
        state: AllocatorState | None = None,
    ):

        self.config = config or DEFAULT_ALLOCATOR_CONFIG
        self.state = state or allocator_state

    # --------------------------------------------------------
    # allocate
    # --------------------------------------------------------

    def allocate(
        self,
        *,
        active: Any = None,
        light: Any = None,
        ranking=None,
        ranking_df=None,
        opening=None,
        positions=None,
        pending_orders=None,
    ) -> AllocationResult:
        """
        push slot allocation
        """

        try:

            # ------------------------------------------------
            # prepare candidates
            # ------------------------------------------------

            candidates = prepare_candidates(
                active=active,
                light=light,
                ranking=ranking,
                ranking_df=ranking_df,
                opening=opening,
                positions=positions,
                pending_orders=pending_orders,
                config=self.config,
            )

            # ------------------------------------------------
            # annotate
            # ------------------------------------------------

            annotated_df = annotate_candidates(
                candidates,
                self.config,
            )

            # ------------------------------------------------
            # scoring
            # ------------------------------------------------

            scored_df = apply_scoring(
                annotated_df,
                self.config,
                self.state,
            )

            # ------------------------------------------------
            # select
            # ------------------------------------------------

            selected = select_symbols(
                scored_df,
                self.config,
                self.state,
            )

            # ------------------------------------------------
            # governor
            # ------------------------------------------------

            governed = apply_governor(
                selected,
                self.config,
                self.state,
            )

            # ------------------------------------------------
            # result
            # ------------------------------------------------

            result = build_result(
                governed,
                self.state,
            )

            logger.info(
                "[push allocator] "
                f"symbols={len(result.symbols)} "
                f"added={len(result.added)} "
                f"removed={len(result.removed)}"
            )

            return result

        except Exception:

            logger.exception("[push allocator] failed")

            # fallback: keep current symbols
            return build_result(
                self.state.current_symbols(),
                self.state,
            )


# ============================================================
# global allocator
# ============================================================

push_slot_allocator = PushSlotAllocator()