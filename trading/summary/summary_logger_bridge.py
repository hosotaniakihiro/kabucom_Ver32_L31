# ============================================================
# File   : trading/summary/summary_logger_bridge.py
# Version: Ver4-PRODUCTION-SAFE-BRIDGE-SIGNATURE-COMPAT
# ------------------------------------------------------------
# ✔ import path fixed
# ✔ summary_analysis_logger actual location used
# ✔ legacy bridge names supported
# ✔ summary_controller positional call signature supported
# ✔ run_summary_loggers(df, interval) supported
# ✔ log_summary_ranking_bridge(df, interval) supported
# ✔ optional entry_df support
# ✔ startup crash prevention
# ============================================================

from __future__ import annotations

import logging
from typing import Any, Optional

import pandas as pd

logger = logging.getLogger(__name__)

# ============================================================
# safe import
# ============================================================

try:
    from trading.summary.summary_analysis_logger import (
        log_summary_analysis,
        print_summary_analysis,
        log_summary_ranking_analysis,
        verify_summary_vs_entry,
        log_summary_ranking,
        print_summary_ranking,
        analyze_summary_ranking,
    )
except Exception as e:
    logger.exception("[summary_logger_bridge] failed to import summary_analysis_logger: %s", e)

    def log_summary_analysis(*args: Any, **kwargs: Any) -> pd.DataFrame:
        return pd.DataFrame()

    def print_summary_analysis(*args: Any, **kwargs: Any) -> pd.DataFrame:
        return pd.DataFrame()

    def log_summary_ranking_analysis(*args: Any, **kwargs: Any) -> pd.DataFrame:
        return pd.DataFrame()

    def verify_summary_vs_entry(*args: Any, **kwargs: Any) -> pd.DataFrame:
        return pd.DataFrame()

    def log_summary_ranking(*args: Any, **kwargs: Any) -> pd.DataFrame:
        return pd.DataFrame()

    def print_summary_ranking(*args: Any, **kwargs: Any) -> pd.DataFrame:
        return pd.DataFrame()

    def analyze_summary_ranking(*args: Any, **kwargs: Any) -> pd.DataFrame:
        return pd.DataFrame()


# ============================================================
# helpers
# ============================================================

def _safe_df(obj: Any) -> pd.DataFrame:
    if isinstance(obj, pd.DataFrame):
        return obj
    try:
        if obj is None:
            return pd.DataFrame()
        return pd.DataFrame(obj)
    except Exception:
        return pd.DataFrame()


def _run_summary_core(
    summary_df: Optional[pd.DataFrame] = None,
    interval: Optional[int | str] = None,
    entry_df: Optional[pd.DataFrame] = None,
    top_n: int = 10,
    min_volume: float = 1.0,
) -> pd.DataFrame:
    try:
        df = _safe_df(summary_df)
        if df.empty:
            logger.info("[summary_logger_bridge] summary logger skipped: empty summary_df")
            return pd.DataFrame()

        prepared = log_summary_analysis(
            df=df,
            interval=interval,
            top_n=top_n,
            min_volume=min_volume,
            apply_market_filter=True,
            apply_name_filter=True,
        )

        try:
            verify_summary_vs_entry(
                summary_df=prepared,
                entry_df=_safe_df(entry_df),
                interval=interval,
                top_n=top_n,
                min_volume=min_volume,
            )
        except Exception:
            logger.exception("[summary_logger_bridge] verify_summary_vs_entry failed")

        return prepared

    except Exception:
        logger.exception("[summary_logger_bridge] _run_summary_core failed")
        return pd.DataFrame()


def _run_ranking_core(
    ranking_df: Optional[pd.DataFrame] = None,
    interval: Optional[int | str] = None,
    top_n: int = 10,
    min_volume: float = 1.0,
) -> pd.DataFrame:
    try:
        df = _safe_df(ranking_df)
        if df.empty:
            logger.info("[summary_logger_bridge] ranking logger skipped: empty ranking_df")
            return pd.DataFrame()

        prepared = log_summary_ranking_analysis(
            df=df,
            interval=interval,
            top_n=top_n,
            min_volume=min_volume,
        )
        return prepared

    except Exception:
        logger.exception("[summary_logger_bridge] _run_ranking_core failed")
        return pd.DataFrame()


# ============================================================
# public compatibility api
# ============================================================

def run_summary_loggers(
    summary_df: Optional[pd.DataFrame] = None,
    interval: Optional[int | str] = None,
    entry_df: Optional[pd.DataFrame] = None,
    top_n: int = 10,
    min_volume: float = 1.0,
    **kwargs: Any,
) -> pd.DataFrame:
    """
    summary_controller.py compatible:
        run_summary_loggers(df, interval)

    Also supports:
        run_summary_loggers(df, interval, entry_df=...)
    """
    return _run_summary_core(
        summary_df=summary_df,
        interval=interval,
        entry_df=entry_df,
        top_n=top_n,
        min_volume=min_volume,
    )


def log_summary_ranking_bridge(
    ranking_df: Optional[pd.DataFrame] = None,
    interval: Optional[int | str] = None,
    top_n: int = 10,
    min_volume: float = 1.0,
    **kwargs: Any,
) -> pd.DataFrame:
    """
    summary_controller.py compatible:
        log_summary_ranking_bridge(df, interval)
    """
    return _run_ranking_core(
        ranking_df=ranking_df,
        interval=interval,
        top_n=top_n,
        min_volume=min_volume,
    )


def run_ranking_loggers(
    ranking_df: Optional[pd.DataFrame] = None,
    interval: Optional[int | str] = None,
    top_n: int = 10,
    min_volume: float = 1.0,
    **kwargs: Any,
) -> pd.DataFrame:
    return _run_ranking_core(
        ranking_df=ranking_df,
        interval=interval,
        top_n=top_n,
        min_volume=min_volume,
    )


def run_all_loggers(
    summary_df: Optional[pd.DataFrame] = None,
    interval: Optional[int | str] = None,
    entry_df: Optional[pd.DataFrame] = None,
    ranking_df: Optional[pd.DataFrame] = None,
    top_n: int = 10,
    min_volume: float = 1.0,
    **kwargs: Any,
) -> dict[str, pd.DataFrame]:
    return {
        "summary": _run_summary_core(
            summary_df=summary_df,
            interval=interval,
            entry_df=entry_df,
            top_n=top_n,
            min_volume=min_volume,
        ),
        "ranking": _run_ranking_core(
            ranking_df=ranking_df,
            interval=interval,
            top_n=top_n,
            min_volume=min_volume,
        ),
    }


def log_summary_bridge(
    summary_df: Optional[pd.DataFrame] = None,
    interval: Optional[int | str] = None,
    top_n: int = 10,
    min_volume: float = 1.0,
    **kwargs: Any,
) -> pd.DataFrame:
    return _run_summary_core(
        summary_df=summary_df,
        interval=interval,
        entry_df=None,
        top_n=top_n,
        min_volume=min_volume,
    )


def run_summary_analysis_logger(
    summary_df: Optional[pd.DataFrame] = None,
    interval: Optional[int | str] = None,
    top_n: int = 10,
    min_volume: float = 1.0,
    **kwargs: Any,
) -> pd.DataFrame:
    return _run_summary_core(
        summary_df=summary_df,
        interval=interval,
        entry_df=None,
        top_n=top_n,
        min_volume=min_volume,
    )


def run_ranking_analysis_logger(
    ranking_df: Optional[pd.DataFrame] = None,
    interval: Optional[int | str] = None,
    top_n: int = 10,
    min_volume: float = 1.0,
    **kwargs: Any,
) -> pd.DataFrame:
    return _run_ranking_core(
        ranking_df=ranking_df,
        interval=interval,
        top_n=top_n,
        min_volume=min_volume,
    )