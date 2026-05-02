# ============================================================
# ats_promotions.py
# Ver1.0-PRODUCTION-RANKING-PROMOTION
# ------------------------------------------------------------
# ✔ ranking promotion
# ✔ summary_cache対応
# ✔ ETF除外
# ✔ symbol_flags除外
# ✔ 重複除去
# ✔ NaN / None 安全
# ✔ 本番例外耐性
# ============================================================

import logging
from typing import List

from global_state import global_data

from trading.push.promotion_from_ranking import (
    select_push_candidates_from_ranking,
)

from ats.ats_filters import (
    filter_symbol_flags,
    filter_low_liquidity,
    filter_etf_guard,
)

logger = logging.getLogger(__name__)


# ============================================================
# 内部ユーティリティ
# ============================================================

def _unique_keep_order(seq: List[str]) -> List[str]:
    return list(dict.fromkeys([str(s) for s in seq if s]))


# ============================================================
# ranking DataFrame取得
# ============================================================

def _get_ranking_df():

    summary_cache = getattr(global_data, "summary_cache", {})

    if not isinstance(summary_cache, dict):
        return None

    ranking_df = summary_cache.get("1min")

    if ranking_df is None:
        return None

    try:

        if ranking_df.empty:
            return None

    except Exception:

        return None

    return ranking_df


# ============================================================
# ranking promotion
# ============================================================

def get_ranking_promotions(max_candidates: int = 10) -> List[str]:

    ranking_df = _get_ranking_df()

    if ranking_df is None:
        return []

    try:

        promoted = select_push_candidates_from_ranking(
            ranking_df,
            max_candidates=max_candidates,
        )

        promoted = [str(s) for s in promoted if s]

        promoted = _unique_keep_order(promoted)

        logger.info(
            "[ATS Promotion] raw_candidates=%d",
            len(promoted),
        )

        return promoted

    except Exception:

        logger.exception("ranking promotion failed")

        return []


# ============================================================
# promotion + フィルター
# ============================================================

def build_promotions(max_candidates: int = 10) -> List[str]:

    try:

        promoted = get_ranking_promotions(max_candidates)

        if not promoted:
            return []

        # ETF除外
        promoted = filter_etf_guard(promoted)

        # symbol_flags
        promoted = filter_symbol_flags(promoted)

        # 流動性
        promoted = filter_low_liquidity(promoted)

        promoted = _unique_keep_order(promoted)

        logger.info(
            "[ATS Promotion] filtered=%d",
            len(promoted),
        )

        return promoted

    except Exception:

        logger.exception("build_promotions failed")

        return []