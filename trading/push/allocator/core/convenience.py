# ============================================================
# File   : trading/push/allocator/convenience.py
# Version: Ver1.0-PRODUCTION-PUSH-ALLOCATOR-CONVENIENCE
# ------------------------------------------------------------
# ✔ convenience wrapper for allocator pipeline
# ✔ single call allocator execution
# ✔ runtime safe
# ✔ production ready
# ============================================================

from __future__ import annotations

import logging
from typing import Any

from trading.push.allocator.config import DEFAULT_ALLOCATOR_CONFIG
from trading.push.allocator.state.state import allocator_state
from trading.push.allocator.core.candidate_preparer import prepare_candidates
from trading.push.allocator.core.annotator import annotate_candidates
from trading.push.allocator.scoring.scoring import apply_scoring
from trading.push.allocator.core.selector import select_symbols
from trading.push.allocator.governance.governor import apply_governor
from trading.push.allocator.core.result_builder import build_result

logger = logging.getLogger(__name__)


# ============================================================
# run allocator
# ============================================================

def run_allocator(
    *,
    active: Any = None,
    light: Any = None,
    ranking=None,
    ranking_df=None,
    opening=None,
    positions=None,
    pending_orders=None,
    config=None,
    state=None,
):
    """
    push allocator 実行
    """

    if config is None:
        config = DEFAULT_ALLOCATOR_CONFIG

    if state is None:
        state = allocator_state

    try:

        # ----------------------------------------------------
        # candidate prepare
        # ----------------------------------------------------

        candidates = prepare_candidates(
            active=active,
            light=light,
            ranking=ranking,
            ranking_df=ranking_df,
            opening=opening,
            positions=positions,
            pending_orders=pending_orders,
            config=config,
        )

        # ----------------------------------------------------
        # annotate
        # ----------------------------------------------------

        annotated_df = annotate_candidates(
            candidates,
            config,
        )

        # ----------------------------------------------------
        # scoring
        # ----------------------------------------------------

        scored_df = apply_scoring(
            annotated_df,
            config,
            state,
        )

        # ----------------------------------------------------
        # selection
        # ----------------------------------------------------

        selected = select_symbols(
            scored_df,
            config,
            state,
        )

        # ----------------------------------------------------
        # governor
        # ----------------------------------------------------

        governed = apply_governor(
            selected,
            config,
            state,
        )

        # ----------------------------------------------------
        # result
        # ----------------------------------------------------

        result = build_result(
            governed,
            state,
        )

        return result

    except Exception as e:

        logger.exception("[allocator] execution failed")

        # fallback
        return build_result(
            state.current_symbols(),
            state,
        )