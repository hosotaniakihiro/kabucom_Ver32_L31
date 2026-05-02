# ============================================================
# File   : utils/df_guard/extractor.py
# Version: Ver1.0-INSTITUTIONAL-DATAFRAME-EXTRACTOR
# ------------------------------------------------------------
# ✔ symbol単位の最新行抽出
# ✔ datetimeベース抽出
# ✔ topN抽出
# ✔ 安定ソート（mergesort）
# ✔ 欠損安全
# ✔ production safe
# ============================================================

from __future__ import annotations

import logging
import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# latest by symbol（最重要）
# ============================================================

def extract_latest_by_symbol(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return pd.DataFrame()

    if "symbol" not in df.columns:
        return df

    if "datetime" not in df.columns:
        return df

    try:

        df = (
            df.sort_values(
                ["symbol", "datetime"],
                kind="mergesort"
            )
            .drop_duplicates(
                subset=["symbol"],
                keep="last"
            )
            .reset_index(drop=True)
        )

    except Exception as e:

        logger.warning(
            "[EXTRACTOR] latest_by_symbol failed: %s", e
        )

    return df


# ============================================================
# latest N rows（全体）
# ============================================================

def extract_latest_n(
    df: pd.DataFrame,
    n: int = 100
) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    try:

        if "datetime" in df.columns:

            return df.sort_values(
                "datetime",
                ascending=False,
                kind="mergesort"
            ).head(n)

        return df.tail(n)

    except Exception as e:

        logger.warning(
            "[EXTRACTOR] latest_n failed: %s", e
        )

        return df


# ============================================================
# extract by datetime range
# ============================================================

def extract_between(
    df: pd.DataFrame,
    start=None,
    end=None
) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    if "datetime" not in df.columns:
        return df

    try:

        cond = pd.Series(True, index=df.index)

        if start is not None:
            cond &= df["datetime"] >= start

        if end is not None:
            cond &= df["datetime"] <= end

        return df[cond]

    except Exception as e:

        logger.warning(
            "[EXTRACTOR] between failed: %s", e
        )

        return df


# ============================================================
# extract top N by column
# ============================================================

def extract_top_n(
    df: pd.DataFrame,
    column: str,
    n: int = 10,
    ascending: bool = False
) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    if column not in df.columns:
        return df

    try:

        return df.sort_values(
            column,
            ascending=ascending,
            kind="mergesort"
        ).head(n)

    except Exception as e:

        logger.warning(
            "[EXTRACTOR] top_n failed: %s", e
        )

        return df


# ============================================================
# extract bottom N
# ============================================================

def extract_bottom_n(
    df: pd.DataFrame,
    column: str,
    n: int = 10
) -> pd.DataFrame:

    return extract_top_n(
        df,
        column,
        n=n,
        ascending=True
    )


# ============================================================
# group latest（高速版）
# ============================================================

def extract_latest_by_group(
    df: pd.DataFrame,
    group_cols: list[str]
) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    if "datetime" not in df.columns:
        return df

    try:

        df = df.sort_values(
            group_cols + ["datetime"],
            kind="mergesort"
        )

        return (
            df
            .groupby(group_cols, as_index=False)
            .tail(1)
            .reset_index(drop=True)
        )

    except Exception as e:

        logger.warning(
            "[EXTRACTOR] latest_by_group failed: %s", e
        )

        return df


# ============================================================
# filter symbols
# ============================================================

def filter_symbols(
    df: pd.DataFrame,
    symbols: list[str]
) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    if "symbol" not in df.columns:
        return df

    try:

        return df[df["symbol"].isin(symbols)]

    except Exception as e:

        logger.warning(
            "[EXTRACTOR] filter_symbols failed: %s", e
        )

        return df


# ============================================================
# get latest timestamp
# ============================================================

def get_latest_timestamp(df: pd.DataFrame):

    if df is None or df.empty:
        return None

    if "datetime" not in df.columns:
        return None

    try:
        return df["datetime"].max()
    except Exception:
        return None


# ============================================================
# filter newer than timestamp
# ============================================================

def filter_newer_than(
    df: pd.DataFrame,
    last_dt
) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    if "datetime" not in df.columns:
        return df

    if last_dt is None:
        return df

    try:

        return df[df["datetime"] > last_dt]

    except Exception as e:

        logger.warning(
            "[EXTRACTOR] filter_newer_than failed: %s", e
        )

        return df


# ============================================================
# public API
# ============================================================

__all__ = [
    "extract_latest_by_symbol",
    "extract_latest_n",
    "extract_between",
    "extract_top_n",
    "extract_bottom_n",
    "extract_latest_by_group",
    "filter_symbols",
    "get_latest_timestamp",
    "filter_newer_than",
]