# ============================================================
# File   : trading/ranking/summary/cache.py
# Ver    : PRODUCTION-STABLE-REV1.0-RANKING-SUMMARY-CACHE
# ------------------------------------------------------------
# 【概要】
#   ranking summary の full / latest を cache_store に反映する
# ============================================================

from __future__ import annotations

import logging

import pandas as pd

logger = logging.getLogger(__name__)


try:
    from trading.ranking.summary.cache_store import (
        _ensure_global_slots,
        set_ranking_summary,
        set_latest_ranking_summary,
    )
except Exception:
    _ensure_global_slots = None  # type: ignore
    set_ranking_summary = None  # type: ignore
    set_latest_ranking_summary = None  # type: ignore
    logger.warning(
        "[RANKING SUMMARY RUNNER] cache_store import failed -> cache update disabled",
        exc_info=True,
    )


def update_ranking_summary_cache(
    df: pd.DataFrame,
    latest: pd.DataFrame,
    *,
    interval: int,
) -> None:
    try:
        if callable(_ensure_global_slots):
            _ensure_global_slots()
    except Exception:
        pass

    if df is None:
        df = pd.DataFrame()

    if latest is None:
        latest = pd.DataFrame()

    try:
        if callable(set_ranking_summary):
            set_ranking_summary(interval, df)
            logger.info(
                "[RANKING SUMMARY RUNNER] cache set interval=%s rows=%s",
                interval,
                len(df),
            )
    except Exception:
        logger.exception(
            "[RANKING SUMMARY RUNNER] cache set failed interval=%s",
            interval,
        )

    try:
        if callable(set_latest_ranking_summary):
            set_latest_ranking_summary(interval, latest)
            logger.info(
                "[RANKING SUMMARY RUNNER] latest cache set interval=%s rows=%s",
                interval,
                len(latest),
            )
    except Exception:
        logger.exception(
            "[RANKING SUMMARY RUNNER] latest cache set failed interval=%s",
            interval,
        )