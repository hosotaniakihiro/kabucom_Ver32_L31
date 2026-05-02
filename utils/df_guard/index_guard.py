# ============================================================
# File   : utils/df_guard/index_guard.py
# Version: Ver1.0-INSTITUTIONAL-INDEX-GUARD
# ------------------------------------------------------------
# ✔ duplicate index guard
# ✔ MultiIndex flatten / reset
# ✔ index name safety
# ✔ safe reset_index
# ✔ sort index stable
# ✔ reindex safety
# ✔ pandas crash防止
# ✔ production safe
# ============================================================

from __future__ import annotations

import logging
import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# duplicate index guard
# ============================================================

def remove_duplicate_index(
    df: pd.DataFrame,
    keep: str = "last"
) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    try:

        if df.index.duplicated().any():

            before = len(df)

            df = df[~df.index.duplicated(keep=keep)]

            dropped = before - len(df)

            logger.warning(
                "[INDEX GUARD] duplicated index removed: %s rows",
                dropped
            )

    except Exception as e:

        logger.warning(
            "[INDEX GUARD] remove duplicate index failed: %s", e
        )

    return df


# ============================================================
# reset index safe
# ============================================================

def safe_reset_index(
    df: pd.DataFrame,
    drop: bool = True
) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    try:

        return df.reset_index(drop=drop)

    except Exception as e:

        logger.warning(
            "[INDEX GUARD] reset_index failed: %s", e
        )

        try:
            df = df.copy()
            df.index = range(len(df))
            return df
        except Exception:
            return df


# ============================================================
# flatten multiindex（index）
# ============================================================

def flatten_multiindex_index(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    try:

        if isinstance(df.index, pd.MultiIndex):

            df = df.copy()

            df.index = [
                "_".join(
                    [str(x) for x in idx if x not in (None, "")]
                )
                for idx in df.index.to_flat_index()
            ]

            logger.warning(
                "[INDEX GUARD] MultiIndex index flattened"
            )

    except Exception as e:

        logger.warning(
            "[INDEX GUARD] flatten index failed: %s", e
        )

    return df


# ============================================================
# ensure simple RangeIndex
# ============================================================

def ensure_range_index(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    try:

        if not isinstance(df.index, pd.RangeIndex):

            df = df.copy()
            df.index = range(len(df))

            logger.warning(
                "[INDEX GUARD] converted to RangeIndex"
            )

    except Exception as e:

        logger.warning(
            "[INDEX GUARD] range index failed: %s", e
        )

    return df


# ============================================================
# sort index stable
# ============================================================

def sort_index(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    try:

        return df.sort_index(kind="mergesort")

    except Exception as e:

        logger.warning(
            "[INDEX GUARD] sort_index failed: %s", e
        )

        return df


# ============================================================
# safe reindex（列揃え用途）
# ============================================================

def safe_reindex(
    df: pd.DataFrame,
    index=None,
    columns=None
) -> pd.DataFrame:

    if df is None:
        return pd.DataFrame()

    if df.empty:
        return df

    try:

        return df.reindex(index=index, columns=columns)

    except Exception as e:

        logger.warning(
            "[INDEX GUARD] reindex failed: %s", e
        )

        return df


# ============================================================
# ensure index name
# ============================================================

def ensure_index_name(
    df: pd.DataFrame,
    name: str | None = None
) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    try:

        if df.index.name is None and name is not None:

            df = df.copy()
            df.index.name = name

            logger.warning(
                "[INDEX GUARD] index name set: %s",
                name
            )

    except Exception as e:

        logger.warning(
            "[INDEX GUARD] set index name failed: %s", e
        )

    return df


# ============================================================
# align two DataFrames safely
# ============================================================

def safe_align(
    df1: pd.DataFrame,
    df2: pd.DataFrame,
    axis: int = 0
):

    try:

        return df1.align(df2, axis=axis, copy=False)

    except Exception as e:

        logger.warning(
            "[INDEX GUARD] align failed: %s", e
        )

        return df1, df2


# ============================================================
# FULL PIPELINE
# ============================================================

def ensure_index(df: pd.DataFrame) -> pd.DataFrame:

    if df is None:
        return pd.DataFrame()

    if df.empty:
        return df

    try:

        df = flatten_multiindex_index(df)

        df = remove_duplicate_index(df)

        df = sort_index(df)

        df = ensure_range_index(df)

    except Exception as e:

        logger.exception(
            "[INDEX GUARD] ensure_index failed: %s", e
        )

    return df


# ============================================================
# public API
# ============================================================

__all__ = [
    "remove_duplicate_index",
    "safe_reset_index",
    "flatten_multiindex_index",
    "ensure_range_index",
    "sort_index",
    "safe_reindex",
    "ensure_index_name",
    "safe_align",
    "ensure_index",
]