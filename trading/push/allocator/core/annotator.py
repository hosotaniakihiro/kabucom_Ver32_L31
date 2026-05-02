# ============================================================
# File   : trading/push/allocator/annotator.py
# Version: Ver1.0-PRODUCTION-PUSH-ALLOCATOR-ANNOTATOR
# ------------------------------------------------------------
# ✔ candidate annotation
# ✔ priority scoring
# ✔ flag generation
# ✔ ranking score integration
# ✔ NaN / inf guard
# ✔ production safe
# ============================================================

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from trading.push.allocator.core.candidate_preparer import CandidateSet

logger = logging.getLogger(__name__)


# ============================================================
# annotate candidates
# ============================================================

def annotate_candidates(
    candidates: CandidateSet,
    config,
) -> pd.DataFrame:
    """
    候補銘柄に priority / flags を付与
    """

    rows = []

    for symbol in candidates.symbols:

        # ----------------------------------------------------
        # flags
        # ----------------------------------------------------

        is_position = int(symbol in candidates.positions)
        is_order = int(symbol in candidates.pending_orders)
        is_active = int(symbol in candidates.active)
        is_light = int(symbol in candidates.light)
        is_ranking = int(symbol in candidates.ranking)
        is_opening = int(symbol in candidates.opening)

        # ----------------------------------------------------
        # ranking score
        # ----------------------------------------------------

        rank_score = candidates.scores.get(symbol, 0.0)

        try:
            rank_score = float(rank_score)
        except Exception:
            rank_score = 0.0

        if np.isnan(rank_score) or np.isinf(rank_score):
            rank_score = 0.0

        # ----------------------------------------------------
        # priority score
        # ----------------------------------------------------

        priority = (
            is_position * config.weight_position
            + is_order * config.weight_order_pending
            + is_active * config.weight_active
            + is_light * config.weight_light
            + is_ranking * config.weight_ranking
            + is_opening * config.weight_opening
            + rank_score * config.rank_score_multiplier
        )

        rows.append(
            {
                "symbol": symbol,
                "priority": priority,
                "rank_score": rank_score,
                "flag_position": is_position,
                "flag_order": is_order,
                "flag_active": is_active,
                "flag_light": is_light,
                "flag_ranking": is_ranking,
                "flag_opening": is_opening,
            }
        )

    if not rows:
        return pd.DataFrame(columns=["symbol", "priority"])

    df = pd.DataFrame(rows)

    # --------------------------------------------------------
    # sort by priority
    # --------------------------------------------------------

    df = df.sort_values(
        "priority",
        ascending=False,
        ignore_index=True,
    )

    logger.info(
        "[allocator annotator] "
        f"candidates={len(df)}"
    )

    return df