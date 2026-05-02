# ============================================================
# File   : trading/summary/logging/summary_stats_logger.py
# Version: Ver1.0-PRODUCTION-SUMMARY-STATS-LOGGER
# ------------------------------------------------------------
# ✔ push summary stats logging
# ✔ ranking summary stats logging
# ✔ symbol count logging
# ✔ datetime range logging
# ✔ dataframe memory stats
# ✔ crash-safe logging
# ✔ real-time safe
# ✔ production monitoring support
# ============================================================

from __future__ import annotations

import logging
import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# SAFE SYMBOL COUNT
# ============================================================

def _symbol_count(df: pd.DataFrame):

    if df is None or df.empty:
        return 0

    if "symbol" not in df.columns:
        return 0

    try:
        return df["symbol"].nunique()
    except Exception:
        return 0


# ============================================================
# SAFE DATETIME RANGE
# ============================================================

def _datetime_range(df: pd.DataFrame):

    if df is None or df.empty:
        return None, None

    if "datetime" not in df.columns:
        return None, None

    try:

        return (
            df["datetime"].min(),
            df["datetime"].max()
        )

    except Exception:

        return None, None


# ============================================================
# MEMORY USAGE
# ============================================================

def _memory_usage_mb(df: pd.DataFrame):

    if df is None or df.empty:
        return 0

    try:

        mem = df.memory_usage(deep=True).sum()

        return round(mem / (1024 * 1024), 2)

    except Exception:

        return 0


# ============================================================
# PUSH SUMMARY STATS
# ============================================================

def log_push_summary_stats(df: pd.DataFrame):

    if df is None:
        logger.info("[SUMMARY STATS] push summary: None")
        return

    try:

        rows, cols = df.shape

        symbols = _symbol_count(df)

        start, end = _datetime_range(df)

        mem = _memory_usage_mb(df)

        logger.info(
            "[SUMMARY STATS] push summary | rows=%s cols=%s symbols=%s mem=%sMB",
            rows,
            cols,
            symbols,
            mem
        )

        if start is not None and end is not None:

            logger.debug(
                "[SUMMARY STATS] push datetime range: %s → %s",
                start,
                end
            )

    except Exception:

        logger.warning(
            "[SUMMARY STATS] push summary logging failed"
        )


# ============================================================
# RANKING SUMMARY STATS
# ============================================================

def log_ranking_summary_stats(df: pd.DataFrame):

    if df is None:
        logger.info("[SUMMARY STATS] ranking summary: None")
        return

    try:

        rows, cols = df.shape

        symbols = _symbol_count(df)

        start, end = _datetime_range(df)

        mem = _memory_usage_mb(df)

        logger.info(
            "[SUMMARY STATS] ranking summary | rows=%s cols=%s symbols=%s mem=%sMB",
            rows,
            cols,
            symbols,
            mem
        )

        if start is not None and end is not None:

            logger.debug(
                "[SUMMARY STATS] ranking datetime range: %s → %s",
                start,
                end
            )

    except Exception:

        logger.warning(
            "[SUMMARY STATS] ranking summary logging failed"
        )


# ============================================================
# MAIN LOGGER
# ============================================================

def log_summary_stats(
    push_summary: pd.DataFrame,
    ranking_summary: pd.DataFrame
):

    try:

        log_push_summary_stats(push_summary)

        log_ranking_summary_stats(ranking_summary)

    except Exception:

        logger.warning(
            "[SUMMARY STATS] logging pipeline failed"
        )