# ============================================================
# File   : trading/ranking/utils/latest_bar_selector.py
# Version: Ver1.0-PRODUCTION-LATEST-BAR-SELECTOR
# ------------------------------------------------------------
# ✔ latest bar selection per symbol
# ✔ symbol/datetime guard
# ✔ MultiIndex column safety
# ✔ duplicate column protection
# ✔ NaT datetime removal
# ✔ pandas vectorized
# ✔ production safe
# ============================================================

from __future__ import annotations

import logging
import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# dataframe safety
# ============================================================

def _safe_dataframe(df: pd.DataFrame) -> pd.DataFrame:

    if df is None:
        return pd.DataFrame()

    if not isinstance(df, pd.DataFrame):
        return pd.DataFrame()

    if df.empty:
        return df

    df = df.copy()

    try:
        df = df.reset_index(drop=True)
    except Exception:
        pass

    # MultiIndex columns guard
    if isinstance(df.columns, pd.MultiIndex):

        df.columns = [
            "_".join([str(c) for c in col if c != ""])
            for col in df.columns
        ]

    # duplicate column guard
    if df.columns.duplicated().any():

        dup = df.columns[df.columns.duplicated()].tolist()

        logger.warning(
            "[latest_bar_selector] duplicate columns removed -> %s",
            dup
        )

        df = df.loc[:, ~df.columns.duplicated()]

    return df


# ============================================================
# normalize symbol
# ============================================================

def _normalize_symbol(df: pd.DataFrame) -> pd.DataFrame:

    if "symbol" not in df.columns:
        return df

    df["symbol"] = (
        df["symbol"]
        .astype(str)
        .str.strip()
    )

    return df


# ============================================================
# normalize datetime
# ============================================================

def _normalize_datetime(df: pd.DataFrame) -> pd.DataFrame:

    if "datetime" not in df.columns:
        return df

    df["datetime"] = pd.to_datetime(
        df["datetime"],
        errors="coerce"
    )

    bad = df["datetime"].isna().sum()

    if bad > 0:

        logger.warning(
            "[latest_bar_selector] dropped rows without datetime: %s",
            bad
        )

        df = df.dropna(subset=["datetime"])

    return df


# ============================================================
# latest selector
# ============================================================

def select_latest_bar(df: pd.DataFrame) -> pd.DataFrame:
    """
    Select the latest row per symbol.
    """

    if df is None:
        return pd.DataFrame()

    if not isinstance(df, pd.DataFrame):
        return pd.DataFrame()

    if df.empty:
        return df

    try:

        df = _safe_dataframe(df)

        if df.empty:
            return df

        if "symbol" not in df.columns:

            logger.warning(
                "[latest_bar_selector] symbol column missing"
            )

            return df

        if "datetime" not in df.columns:

            logger.warning(
                "[latest_bar_selector] datetime column missing"
            )

            return df

        df = _normalize_symbol(df)

        df = _normalize_datetime(df)

        if df.empty:
            return df

        # ----------------------------------------------------
        # sort for latest selection
        # ----------------------------------------------------

        df = df.sort_values("datetime")

        # ----------------------------------------------------
        # groupby latest row
        # ----------------------------------------------------

        latest = (
            df
            .groupby("symbol", as_index=False)
            .tail(1)
        )

        latest = latest.reset_index(drop=True)

        logger.debug(
            "[latest_bar_selector] selected latest bars -> %s symbols",
            len(latest)
        )

        return latest

    except Exception:

        logger.exception(
            "[latest_bar_selector] failed"
        )

        return pd.DataFrame()