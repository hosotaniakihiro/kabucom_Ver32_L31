# ============================================================
# File   : trading/ranking/utils/dataframe_guard.py
# Version: Ver1.0-PRODUCTION-DATAFRAME-GUARD
# ------------------------------------------------------------
# ✔ dataframe integrity guard
# ✔ None / invalid input protection
# ✔ MultiIndex column flatten
# ✔ duplicate column removal
# ✔ symbol normalization
# ✔ datetime normalization
# ✔ NaN / inf numeric sanitize
# ✔ dtype stabilization
# ✔ index reset
# ✔ production safe
# ============================================================

from __future__ import annotations

import logging
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# safe dataframe
# ============================================================

def ensure_dataframe(obj) -> pd.DataFrame:
    """
    Ensure object is a DataFrame.
    """

    if obj is None:
        return pd.DataFrame()

    if isinstance(obj, pd.DataFrame):
        return obj

    logger.warning(
        "[dataframe_guard] non-DataFrame input detected: %s",
        type(obj),
    )

    return pd.DataFrame()


# ============================================================
# flatten MultiIndex columns
# ============================================================

def flatten_columns(df: pd.DataFrame) -> pd.DataFrame:

    if isinstance(df.columns, pd.MultiIndex):

        df = df.copy()

        df.columns = [
            "_".join([str(c) for c in col if c != ""])
            for col in df.columns
        ]

        logger.debug("[dataframe_guard] MultiIndex columns flattened")

    return df


# ============================================================
# remove duplicate columns
# ============================================================

def remove_duplicate_columns(df: pd.DataFrame) -> pd.DataFrame:

    if not df.columns.duplicated().any():
        return df

    dup = df.columns[df.columns.duplicated()].tolist()

    logger.warning(
        "[dataframe_guard] duplicate columns removed -> %s",
        dup
    )

    return df.loc[:, ~df.columns.duplicated()]


# ============================================================
# normalize symbol
# ============================================================

def normalize_symbol(df: pd.DataFrame) -> pd.DataFrame:

    if "symbol" not in df.columns:
        return df

    df = df.copy()

    df["symbol"] = (
        df["symbol"]
        .astype(str)
        .str.strip()
    )

    return df


# ============================================================
# normalize datetime
# ============================================================

def normalize_datetime(df: pd.DataFrame) -> pd.DataFrame:

    if "datetime" not in df.columns:
        return df

    df = df.copy()

    df["datetime"] = pd.to_datetime(
        df["datetime"],
        errors="coerce"
    )

    bad = df["datetime"].isna().sum()

    if bad > 0:

        logger.warning(
            "[dataframe_guard] dropped rows without datetime: %s",
            bad
        )

        df = df.dropna(subset=["datetime"])

    return df


# ============================================================
# numeric sanitize
# ============================================================

def sanitize_numeric(df: pd.DataFrame) -> pd.DataFrame:

    if df.empty:
        return df

    num_cols = df.select_dtypes(include=np.number).columns

    if len(num_cols) == 0:
        return df

    df = df.copy()

    df[num_cols] = (
        df[num_cols]
        .replace([np.inf, -np.inf], np.nan)
        .fillna(0)
    )

    return df


# ============================================================
# index reset
# ============================================================

def reset_index_safe(df: pd.DataFrame) -> pd.DataFrame:

    try:
        return df.reset_index(drop=True)
    except Exception:
        logger.exception("[dataframe_guard] reset_index failed")
        return df


# ============================================================
# main guard pipeline
# ============================================================

def guard_dataframe(df) -> pd.DataFrame:
    """
    Full DataFrame guard pipeline.
    """

    df = ensure_dataframe(df)

    if df.empty:
        return df

    try:

        df = reset_index_safe(df)

        df = flatten_columns(df)

        df = remove_duplicate_columns(df)

        df = normalize_symbol(df)

        df = normalize_datetime(df)

        df = sanitize_numeric(df)

        return df

    except Exception:

        logger.exception(
            "[dataframe_guard] dataframe guard failed"
        )

        return pd.DataFrame()