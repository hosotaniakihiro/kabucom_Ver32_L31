# ============================================================
# File   : trading/summary/cache/summary_cache_manager.py
# Version: Ver1.0-PRODUCTION-SUMMARY-CACHE-MANAGER
# ------------------------------------------------------------
# ✔ push summary cache update
# ✔ ranking summary cache update
# ✔ merged summary cache update
# ✔ cache safety guard
# ✔ dataframe isolation copy
# ✔ runtime crash isolation
# ✔ global_data compatibility
# ✔ production logging
# ============================================================

from __future__ import annotations

import logging
import pandas as pd

from global_state import global_data

logger = logging.getLogger(__name__)


# ============================================================
# SAFE COPY
# ============================================================

def _safe_copy(df: pd.DataFrame) -> pd.DataFrame:

    if df is None:
        return None

    if not isinstance(df, pd.DataFrame):
        return None

    try:
        return df.copy()
    except Exception:
        return df


# ============================================================
# UPDATE PUSH SUMMARY CACHE
# ============================================================

def update_push_summary_cache(df: pd.DataFrame):

    if df is None or df.empty:
        return

    try:

        df = _safe_copy(df)

        if hasattr(global_data, "set_merged_summary"):

            global_data.set_merged_summary(1, df)

        else:

            logger.warning(
                "[SUMMARY CACHE] set_merged_summary not available"
            )

    except Exception:

        logger.exception(
            "[SUMMARY CACHE] push summary cache failed"
        )


# ============================================================
# UPDATE RANKING SUMMARY CACHE
# ============================================================

def update_ranking_summary_cache(df: pd.DataFrame):

    if df is None or df.empty:
        return

    try:

        df = _safe_copy(df)

        if hasattr(global_data, "set_ranking_summary"):

            global_data.set_ranking_summary(df)

        else:

            logger.warning(
                "[SUMMARY CACHE] set_ranking_summary not available"
            )

    except Exception:

        logger.exception(
            "[SUMMARY CACHE] ranking summary cache failed"
        )


# ============================================================
# UPDATE BOTH CACHES
# ============================================================

def update_summary_caches(
    push_summary: pd.DataFrame,
    ranking_summary: pd.DataFrame
):

    try:

        if push_summary is not None and not push_summary.empty:

            update_push_summary_cache(push_summary)

        if ranking_summary is not None and not ranking_summary.empty:

            update_ranking_summary_cache(ranking_summary)

    except Exception:

        logger.exception(
            "[SUMMARY CACHE] cache update failed"
        )


# ============================================================
# GET PUSH SUMMARY
# ============================================================

def get_push_summary():

    try:

        if hasattr(global_data, "get_multi_summary"):

            return global_data.get_multi_summary(1)

    except Exception:

        logger.warning(
            "[SUMMARY CACHE] get push summary failed"
        )

    return None


# ============================================================
# GET RANKING SUMMARY
# ============================================================

def get_ranking_summary():

    try:

        if hasattr(global_data, "get_ranking_summary"):

            return global_data.get_ranking_summary()

    except Exception:

        logger.warning(
            "[SUMMARY CACHE] get ranking summary failed"
        )

    return None


# ============================================================
# CLEAR CACHE
# ============================================================

def clear_summary_cache():

    try:

        if hasattr(global_data, "set_merged_summary"):
            global_data.set_merged_summary(1, None)

        if hasattr(global_data, "set_ranking_summary"):
            global_data.set_ranking_summary(None)

        logger.info("[SUMMARY CACHE] cache cleared")

    except Exception:

        logger.warning(
            "[SUMMARY CACHE] cache clear failed"
        )