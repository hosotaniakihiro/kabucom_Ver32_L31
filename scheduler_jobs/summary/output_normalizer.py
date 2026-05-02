# ============================================================
# File   : scheduler_jobs/summary/output_normalizer.py
# Version: PRODUCTION-STABLE-SUMMARY-OUTPUT-NORMALIZER-V1.0
# ------------------------------------------------------------
# 【概要】
#   summary runner の戻り値を DataFrame + meta に正規化する。
#
# 【主な機能】
#   - DataFrame
#   - tuple(DataFrame, meta)
#   - dict 内 DataFrame
#   - unsupported / None の安全処理
# ============================================================

from __future__ import annotations

import logging
from typing import Any, Optional

import pandas as pd

from .display_prepare import latest_dt_str, symbols_count

logger = logging.getLogger(__name__)


def normalize_runner_output(result: Any) -> tuple[pd.DataFrame, dict]:
    meta: dict = {}

    logger.info(
        "[summary.runners] normalize_runner_output start result_type=%s",
        type(result).__name__,
    )

    if result is None:
        logger.warning("[summary.runners] normalize_runner_output: result is None")
        return pd.DataFrame(), meta

    if isinstance(result, pd.DataFrame):
        logger.info(
            "[summary.runners] normalize_runner_output: dataframe rows=%d cols=%d",
            len(result),
            len(result.columns),
        )
        return result.copy(), meta

    if isinstance(result, tuple):
        logger.info(
            "[summary.runners] normalize_runner_output: tuple len=%d",
            len(result),
        )

        if len(result) >= 1 and isinstance(result[0], pd.DataFrame):
            if len(result) >= 2 and isinstance(result[1], dict):
                meta = result[1].copy()

            logger.info(
                "[summary.runners] normalize_runner_output: tuple[0] dataframe rows=%d meta_keys=%s",
                len(result[0]),
                sorted(list(meta.keys())) if isinstance(meta, dict) else [],
            )
            return result[0].copy(), meta

        logger.warning("[summary.runners] normalize_runner_output: tuple but dataframe not found")
        return pd.DataFrame(), meta

    if isinstance(result, dict):
        meta = result.copy()

        logger.info(
            "[summary.runners] normalize_runner_output: dict keys=%s",
            sorted(list(result.keys())),
        )

        for key in (
            "result_df",
            "merged_df",
            "df",
            "summary_df",
            "output_df",
            "display_df",
            "latest_df",
            "latest_summary_df",
            "summary_latest_df",
        ):
            val = result.get(key)
            if isinstance(val, pd.DataFrame):
                logger.info(
                    "[summary.runners] normalize_runner_output: dataframe found in dict key=%s rows=%d",
                    key,
                    len(val),
                )
                return val.copy(), meta

        logger.warning("[summary.runners] normalize_runner_output: dict but dataframe key not found")
        return pd.DataFrame(), meta

    logger.warning(
        "[summary.runners] normalize_runner_output: unsupported result_type=%s",
        type(result).__name__,
    )
    return pd.DataFrame(), meta


def log_job_result(
    label: str,
    interval: int,
    df: pd.DataFrame,
    meta: Optional[dict] = None,
) -> None:
    meta = meta or {}

    logger.info(
        "[summary.runners] %s done interval=%s rows=%d symbols=%d latest_dt=%s meta_keys=%s",
        label,
        interval,
        len(df) if isinstance(df, pd.DataFrame) else 0,
        symbols_count(df),
        latest_dt_str(df),
        sorted(list(meta.keys())) if isinstance(meta, dict) else [],
    )