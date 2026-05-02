# ============================================================
# File   : trading/push/allocator/candidate_preparer.py
# Version: Ver1.0-PRODUCTION-PUSH-CANDIDATE-PREPARER
# ------------------------------------------------------------
# ✔ candidate symbol preparation
# ✔ ACTIVE / LIGHT / RANKING / OPENING merge
# ✔ position / pending order integration
# ✔ DataFrame / list / set safe
# ✔ score map generation
# ✔ ETF filter
# ✔ production safe
# ============================================================

from __future__ import annotations

import logging
from typing import Dict, Set, Any

import pandas as pd

from trading.push.allocator.utils import (
    extract_symbols,
    dataframe_score_map,
    merge_symbol_sets,
    filter_etf,
)

logger = logging.getLogger(__name__)


# ============================================================
# candidate result
# ============================================================

class CandidateSet:
    """
    allocator入力構造
    """

    def __init__(
        self,
        symbols: Set[str],
        scores: Dict[str, float],
        active: Set[str],
        light: Set[str],
        ranking: Set[str],
        opening: Set[str],
        positions: Set[str],
        pending_orders: Set[str],
    ):
        self.symbols = symbols
        self.scores = scores
        self.active = active
        self.light = light
        self.ranking = ranking
        self.opening = opening
        self.positions = positions
        self.pending_orders = pending_orders


# ============================================================
# prepare candidates
# ============================================================

def prepare_candidates(
    *,
    active: Any = None,
    light: Any = None,
    ranking: Any = None,
    ranking_df: pd.DataFrame | None = None,
    opening: Any = None,
    positions: Any = None,
    pending_orders: Any = None,
    config=None,
) -> CandidateSet:
    """
    push allocator 用候補銘柄生成
    """

    # --------------------------------------------------------
    # symbol extraction
    # --------------------------------------------------------

    active_set = extract_symbols(active)
    light_set = extract_symbols(light)
    ranking_set = extract_symbols(ranking)
    opening_set = extract_symbols(opening)
    position_set = extract_symbols(positions)
    order_set = extract_symbols(pending_orders)

    # ranking_df から symbol 追加
    if ranking_df is not None:
        ranking_set |= extract_symbols(ranking_df)

    # --------------------------------------------------------
    # score map
    # --------------------------------------------------------

    score_map: Dict[str, float] = {}

    if ranking_df is not None:

        # score_buy 優先
        if "score_buy" in ranking_df.columns:
            score_map = dataframe_score_map(ranking_df, "score_buy")

        elif "score" in ranking_df.columns:
            score_map = dataframe_score_map(ranking_df, "score")

    # --------------------------------------------------------
    # merge candidates
    # --------------------------------------------------------

    candidate_symbols = merge_symbol_sets(
        active_set,
        light_set,
        ranking_set,
        opening_set,
        position_set,
        order_set,
    )

    # --------------------------------------------------------
    # ETF filter
    # --------------------------------------------------------

    if config is not None:
        candidate_symbols = filter_etf(
            candidate_symbols,
            getattr(config, "etf_prefix", ()),
        )

    logger.info(
        "[candidate_preparer] "
        f"active={len(active_set)} "
        f"light={len(light_set)} "
        f"ranking={len(ranking_set)} "
        f"opening={len(opening_set)} "
        f"positions={len(position_set)} "
        f"orders={len(order_set)} "
        f"candidates={len(candidate_symbols)}"
    )

    # --------------------------------------------------------
    # return structure
    # --------------------------------------------------------

    return CandidateSet(
        symbols=candidate_symbols,
        scores=score_map,
        active=active_set,
        light=light_set,
        ranking=ranking_set,
        opening=opening_set,
        positions=position_set,
        pending_orders=order_set,
    )