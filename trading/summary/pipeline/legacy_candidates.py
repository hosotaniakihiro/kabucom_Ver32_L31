# ============================================================
# File   : trading/summary/pipeline/legacy_candidates.py
# Version: Ver32_L05-SPLIT-LEGACY-CANDIDATES
# Purpose:
#   summary_pipeline から呼ぶ legacy engine/controller 候補群
# ============================================================

from __future__ import annotations

import inspect
import logging
from typing import Any, Optional

import pandas as pd

from .dataframe_safe import safe_latest_dt, safe_symbols

logger = logging.getLogger(__name__)


def call_with_supported_kwargs(fn, *args, **kwargs):
    sig = inspect.signature(fn)
    allowed = set(sig.parameters.keys())
    filtered = {k: v for k, v in kwargs.items() if k in allowed}
    return fn(*args, **filtered)


def normalize_candidate_output(obj: Any) -> Optional[pd.DataFrame]:
    if isinstance(obj, pd.DataFrame):
        return obj.copy()

    if isinstance(obj, dict):
        for key in (
            "summary_latest_df",
            "summary_df",
            "df",
            "data",
            "result_df",
            "merged_df",
            "output_df",
        ):
            val = obj.get(key)
            if isinstance(val, pd.DataFrame):
                return val.copy()
            if isinstance(val, pd.Series):
                return val.to_frame().T.reset_index(drop=True)
        return pd.DataFrame()

    if isinstance(obj, pd.Series):
        return obj.to_frame().T.reset_index(drop=True)

    if isinstance(obj, tuple) and len(obj) >= 1:
        if isinstance(obj[0], pd.DataFrame):
            return obj[0].copy()

    return None


def try_incremental_engine(
    summary_df: pd.DataFrame,
    push_df: pd.DataFrame,
    *,
    interval: int,
    evaluate_signals: bool,
    latest_only: bool,
    recent_bars_per_symbol: int,
) -> Optional[pd.DataFrame]:
    try:
        from trading.summary.engine.summary_incremental_engine import (
            run_incremental_summary_engine,
        )

        out = call_with_supported_kwargs(
            run_incremental_summary_engine,
            interval=interval,
            summary_df=summary_df,
            push_df=push_df,
            evaluate_signals=evaluate_signals,
            latest_only=latest_only,
            recent_bars_per_symbol=recent_bars_per_symbol,
        )

        df = normalize_candidate_output(out)

        if isinstance(df, pd.DataFrame):
            logger.info(
                "[summary_pipeline] candidate ok -> incremental_engine interval=%s rows=%s symbols=%s latest_dt=%s",
                interval,
                len(df),
                safe_symbols(df),
                safe_latest_dt(df),
            )
            return df

        return None

    except Exception as e:
        logger.error(
            "[summary_pipeline] candidate failed -> incremental_engine interval=%s err=%s: %s",
            interval,
            type(e).__name__,
            str(e)[:300],
            exc_info=False,
        )
        return None


def try_summary_controller(
    summary_df: pd.DataFrame,
    push_df: pd.DataFrame,
    *,
    interval: int,
    evaluate_signals: bool,
    latest_only: bool,
    recent_bars_per_symbol: int,
) -> Optional[pd.DataFrame]:
    try:
        from trading.summary.summary_controller import summary_controller

        df = summary_controller.diff_update(interval=interval)

        if isinstance(df, pd.DataFrame):
            logger.info(
                "[summary_pipeline] candidate ok -> summary_controller.diff_update interval=%s rows=%s symbols=%s latest_dt=%s",
                interval,
                len(df),
                safe_symbols(df),
                safe_latest_dt(df),
            )
            return df

        logger.warning(
            "[summary_pipeline] candidate empty/non-df -> summary_controller.diff_update interval=%s type=%s",
            interval,
            type(df).__name__,
        )
        return None

    except Exception as e:
        logger.error(
            "[summary_pipeline] candidate failed -> summary_controller.diff_update interval=%s err=%s: %s",
            interval,
            type(e).__name__,
            str(e)[:300],
            exc_info=False,
        )
        return None


def try_summary_engine(
    summary_df: pd.DataFrame,
    push_df: pd.DataFrame,
    *,
    interval: int,
    evaluate_signals: bool,
    latest_only: bool,
    recent_bars_per_symbol: int,
) -> Optional[pd.DataFrame]:
    try:
        from trading.summary.engine.summary_engine import run_summary_engine

        out = call_with_supported_kwargs(
            run_summary_engine,
            interval=interval,
            summary_df=summary_df,
            push_df=push_df,
            evaluate_signals=evaluate_signals,
            latest_only=latest_only,
            recent_bars_per_symbol=recent_bars_per_symbol,
        )

        df = normalize_candidate_output(out)

        if isinstance(df, pd.DataFrame):
            logger.info(
                "[summary_pipeline] candidate ok -> summary_engine interval=%s rows=%s symbols=%s latest_dt=%s",
                interval,
                len(df),
                safe_symbols(df),
                safe_latest_dt(df),
            )
            return df

        return None

    except Exception as e:
        logger.error(
            "[summary_pipeline] candidate failed -> summary_engine interval=%s err=%s: %s",
            interval,
            type(e).__name__,
            str(e)[:300],
            exc_info=False,
        )
        return None


__all__ = [
    "try_incremental_engine",
    "try_summary_controller",
    "try_summary_engine",
]
