# ============================================================
# File   : trading/summary/utils/duplicate_guard.py
# Version: Ver1.0-PRODUCTION-DUPLICATE-GUARD
# ------------------------------------------------------------
# ✔ symbol + datetime duplicate row guard
# ✔ duplicate column guard
# ✔ OHLC duplicate column guard
# ✔ DataFrame index duplicate guard
# ✔ pandas alignment crash防止
# ✔ UPSERT integrity crash防止
# ✔ production logging
# ============================================================

from __future__ import annotations

import logging
import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# REMOVE DUPLICATE COLUMNS
# ============================================================

def remove_duplicate_columns(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    if df.columns.duplicated().any():

        dup = df.columns[df.columns.duplicated()].tolist()

        logger.warning(
            "[DUPLICATE GUARD] duplicate columns removed: %s",
            dup
        )

        df = df.loc[:, ~df.columns.duplicated()].copy()

    return df


# ============================================================
# REMOVE DUPLICATE INDEX
# ============================================================

def remove_duplicate_index(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    if df.index.duplicated().any():

        logger.warning(
            "[DUPLICATE GUARD] duplicate index removed"
        )

        df = df[~df.index.duplicated(keep="last")]

    return df


# ============================================================
# REMOVE SYMBOL + DATETIME DUPLICATE ROWS
# ============================================================

def remove_symbol_datetime_duplicates(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    if "symbol" not in df.columns or "datetime" not in df.columns:
        return df

    before = len(df)

    try:

        df = df.drop_duplicates(
            subset=["symbol", "datetime"],
            keep="last"
        )

    except Exception:

        logger.warning(
            "[DUPLICATE GUARD] symbol-datetime duplicate removal failed"
        )

        return df

    removed = before - len(df)

    if removed > 0:

        logger.warning(
            "[DUPLICATE GUARD] removed %s duplicate rows (symbol+datetime)",
            removed
        )

    return df


# ============================================================
# REMOVE OHLC DUPLICATE COLUMNS
# ============================================================

def remove_ohlc_duplicate_columns(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    for c in ["open", "high", "low", "close"]:

        idx = [
            i for i, col in enumerate(df.columns)
            if col == c
        ]

        if len(idx) <= 1:
            continue

        logger.warning(
            "[DUPLICATE GUARD] duplicate OHLC column removed: %s",
            c
        )

        df = df.drop(df.columns[idx[1:]], axis=1)

    return df


# ============================================================
# FULL DUPLICATE GUARD
# ============================================================

def guard_duplicates(df: pd.DataFrame) -> pd.DataFrame:

    if df is None:
        return pd.DataFrame()

    if not isinstance(df, pd.DataFrame):

        try:
            df = pd.DataFrame(df)
        except Exception:
            return pd.DataFrame()

    if df.empty:
        return df

    try:

        df = remove_duplicate_columns(df)

        df = remove_duplicate_index(df)

        df = remove_ohlc_duplicate_columns(df)

        df = remove_symbol_datetime_duplicates(df)

        df = df.reset_index(drop=True)

    except Exception:

        logger.exception(
            "[DUPLICATE GUARD] unexpected failure"
        )

    return df