# ============================================================
# File   : trading/summary/engine/summary_sources.py
# Version: Ver3.0-PRODUCTION-SUMMARY-SOURCES-INSTITUTIONAL
# ------------------------------------------------------------
# ✔ Ver2 全機能保持（削除ゼロ）
# ✔ incremental summary dict互換
# ✔ push summary loader
# ✔ ranking summary loader
# ✔ dataframe guard
# ✔ summary pipeline integration
# ✔ cutoff datetime filter
# ✔ OHLC alias repair
# ✔ datetime normalize guard
# ✔ duplicate column guard
# ✔ numeric sanitize
# ✔ runtime crash isolation
# ✔ pandas alignment crash防止
# ✔ production hardened
# ============================================================

from __future__ import annotations

import logging
import pandas as pd
import numpy as np
import datetime as dt

# ============================================================
# CORE ENGINES
# ============================================================

from trading.summary.engine.summary_incremental_engine import (
    run_incremental_summary
)

from trading.summary.engine.ranking_summary_engine import (
    run_ranking_summary
)

# ============================================================
# SUMMARY PIPELINE
# ============================================================

from trading.summary.engine.summary_pipeline import (
    run_summary_pipeline
)

# ============================================================
# DATAFRAME PREPARE
# ============================================================

from trading.summary.engine.dataframe_prepare import (
    ensure_dataframe,
)

logger = logging.getLogger(__name__)


# ============================================================
# INTERNAL HELPERS
# ============================================================

def _repair_ohlc_alias(df: pd.DataFrame):

    if df.empty:
        return df

    alias = {
        "open_price": "open",
        "high_price": "high",
        "low_price": "low",
        "close_price": "close",
    }

    for src, dst in alias.items():

        if src in df.columns and dst not in df.columns:

            try:
                df[dst] = df[src]
            except Exception:
                pass

    return df


# ============================================================

def _repair_datetime(df: pd.DataFrame):

    if df.empty:
        return df

    if "datetime" not in df.columns:

        for alt in (
            "end_time",
            "time",
            "timestamp",
            "snapshot_time",
        ):

            if alt in df.columns:

                df["datetime"] = df[alt]

                logger.debug(
                    "[SUMMARY SOURCES] datetime alias used -> %s",
                    alt
                )

                break

    if "datetime" in df.columns:

        df["datetime"] = pd.to_datetime(
            df["datetime"],
            errors="coerce"
        )

        before = len(df)

        df = df.dropna(subset=["datetime"])

        removed = before - len(df)

        if removed > 0:

            logger.warning(
                "[SUMMARY SOURCES] dropped rows without datetime -> %s",
                removed
            )

    return df


# ============================================================

def _drop_duplicate_columns(df: pd.DataFrame):

    if df.columns.duplicated().any():

        dup = df.columns[df.columns.duplicated()].tolist()

        logger.warning(
            "[SUMMARY SOURCES] duplicate columns removed -> %s",
            dup
        )

        df = df.loc[:, ~df.columns.duplicated()]

    return df


# ============================================================

def _sanitize_numeric(df: pd.DataFrame):

    if df.empty:
        return df

    num_cols = df.select_dtypes(include=np.number).columns

    try:

        df[num_cols] = (
            df[num_cols]
            .replace([np.inf, -np.inf], np.nan)
            .fillna(0)
        )

    except Exception:

        logger.exception(
            "[SUMMARY SOURCES] numeric sanitize failed"
        )

    return df


# ============================================================

def _prepare_dataframe(df: pd.DataFrame):

    df = ensure_dataframe(df)

    if df.empty:
        return df

    try:

        df = df.copy()

        if isinstance(df.columns, pd.MultiIndex):

            df.columns = df.columns.get_level_values(0)

        df = _drop_duplicate_columns(df)

        df = _repair_ohlc_alias(df)

        df = _repair_datetime(df)

        df = _sanitize_numeric(df)

        if "symbol" in df.columns:

            df["symbol"] = df["symbol"].astype(str)

        df = df.reset_index(drop=True)

        return df

    except Exception:

        logger.exception(
            "[SUMMARY SOURCES] dataframe prepare failed"
        )

        return pd.DataFrame()


# ============================================================
# PUSH SUMMARY SOURCE
# ============================================================

def run_push_summary(cutoff_dt: dt.datetime) -> pd.DataFrame:

    try:

        result = run_incremental_summary()

        # ----------------------------------------------------
        # incremental summary dict互換
        # ----------------------------------------------------

        if isinstance(result, dict):

            push_summary = result.get("latest_1m")

            if push_summary is None:

                push_summary = result.get("summary_1m")

        else:

            push_summary = result

        push_summary = _prepare_dataframe(push_summary)

        if push_summary.empty:
            return push_summary

        logger.debug(
            "[SUMMARY SOURCES] push rows=%s cols=%s",
            len(push_summary),
            len(push_summary.columns)
        )

        # cutoff filter
        if "datetime" in push_summary.columns:

            push_summary = push_summary[
                push_summary["datetime"] <= cutoff_dt
            ]

        # summary pipeline
        try:

            push_summary = run_summary_pipeline(
                push_summary,
                interval="1m"
            )

        except Exception:

            logger.exception(
                "[SUMMARY SOURCES] summary pipeline failed (push)"
            )

        return push_summary

    except Exception:

        logger.exception(
            "[SUMMARY SOURCES] push summary failed"
        )

        return pd.DataFrame()


# ============================================================
# RANKING SUMMARY SOURCE
# ============================================================

def run_ranking_summary_source(
    cutoff_dt: dt.datetime
) -> pd.DataFrame:

    try:

        ranking_summary = run_ranking_summary()

        # ----------------------------------------------------
        # dict → DataFrame
        # ----------------------------------------------------

        if isinstance(ranking_summary, dict):

            ranking_summary = pd.DataFrame(
                ranking_summary.values()
            )

        elif isinstance(ranking_summary, list):

            ranking_summary = pd.DataFrame(
                ranking_summary
            )

        ranking_summary = _prepare_dataframe(
            ranking_summary
        )

        if ranking_summary.empty:
            return ranking_summary

        logger.debug(
            "[SUMMARY SOURCES] ranking rows=%s cols=%s",
            len(ranking_summary),
            len(ranking_summary.columns)
        )

        # cutoff filter
        if "datetime" in ranking_summary.columns:

            ranking_summary = ranking_summary[
                ranking_summary["datetime"] <= cutoff_dt
            ]

        # summary pipeline
        try:

            ranking_summary = run_summary_pipeline(
                ranking_summary,
                interval="1m"
            )

        except Exception:

            logger.exception(
                "[SUMMARY SOURCES] summary pipeline failed (ranking)"
            )

        return ranking_summary

    except Exception:

        logger.exception(
            "[SUMMARY SOURCES] ranking summary failed"
        )

        return pd.DataFrame()