# ============================================================
# File   : trading/summary/utils/dataframe_sanitizer.py
# Version: Ver1.0-PRODUCTION-DATAFRAME-SANITIZER
# ------------------------------------------------------------
# ✔ duplicate column guard
# ✔ OHLC duplicate guard
# ✔ datetime duplicate guard
# ✔ MultiIndex flatten
# ✔ symbol dtype stabilization
# ✔ NaN / inf protection
# ✔ extreme value guard
# ✔ price alias repair
# ✔ index reset safety
# ✔ pandas alignment crash防止
# ✔ production logging
# ============================================================

from __future__ import annotations

import logging
import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)


# ============================================================
# DATETIME DUPLICATE GUARD
# ============================================================

def _fix_datetime_duplicate(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    cols = list(df.columns)

    if cols.count("datetime") <= 1:
        return df

    first = cols.index("datetime")

    drop = [
        i for i, c in enumerate(cols)
        if c == "datetime" and i != first
    ]

    return df.drop(df.columns[drop], axis=1)


# ============================================================
# OHLC DUPLICATE GUARD
# ============================================================

def _force_ohlc_unique(df: pd.DataFrame) -> pd.DataFrame:

    for c in ["open", "high", "low", "close"]:

        idx = [
            i for i, col in enumerate(df.columns)
            if col == c
        ]

        if len(idx) <= 1:
            continue

        df = df.drop(df.columns[idx[1:]], axis=1)

    return df


# ============================================================
# MULTIINDEX FLATTEN
# ============================================================

def _flatten_columns(df: pd.DataFrame) -> pd.DataFrame:

    if isinstance(df.columns, pd.MultiIndex):

        df.columns = [
            "_".join(
                [str(x) for x in col if x not in (None, "")]
            )
            for col in df.columns
        ]

    return df


# ============================================================
# DUPLICATE COLUMN GUARD
# ============================================================

def _remove_duplicate_columns(df: pd.DataFrame) -> pd.DataFrame:

    if df.columns.duplicated().any():

        dup = df.columns[df.columns.duplicated()].tolist()

        logger.warning(
            "[DATAFRAME SANITIZER] duplicate columns removed: %s",
            dup
        )

        df = df.loc[:, ~df.columns.duplicated()].copy()

    return df


# ============================================================
# SYMBOL SAFETY
# ============================================================

def _sanitize_symbol(df: pd.DataFrame) -> pd.DataFrame:

    if "symbol" in df.columns:

        try:
            df["symbol"] = df["symbol"].astype(str)
        except Exception:
            logger.warning(
                "[DATAFRAME SANITIZER] symbol dtype convert failed"
            )

    return df


# ============================================================
# NUMERIC GUARD
# ============================================================

def _sanitize_numeric(df: pd.DataFrame) -> pd.DataFrame:

    try:

        numeric_cols = df.select_dtypes(include=[np.number]).columns

        if len(numeric_cols) == 0:
            return df

        df[numeric_cols] = (
            df[numeric_cols]
            .replace([np.inf, -np.inf], np.nan)
            .fillna(0)
            .clip(-1e12, 1e12)
        )

    except Exception:

        logger.warning(
            "[DATAFRAME SANITIZER] numeric sanitize failed"
        )

    return df


# ============================================================
# PRICE ALIAS REPAIR
# ============================================================

def _repair_price_alias(df: pd.DataFrame) -> pd.DataFrame:

    alias = {
        "close_price": "close",
        "open_price": "open",
        "high_price": "high",
        "low_price": "low",
    }

    for src, dst in alias.items():

        if src in df.columns and dst not in df.columns:

            try:
                df[dst] = df[src]
            except Exception:
                logger.warning(
                    "[DATAFRAME SANITIZER] alias repair failed: %s -> %s",
                    src,
                    dst
                )

    return df


# ============================================================
# INDEX SAFETY
# ============================================================

def _reset_index_safe(df: pd.DataFrame) -> pd.DataFrame:

    try:
        df = df.reset_index(drop=True)
    except Exception:
        pass

    return df


# ============================================================
# MAIN SANITIZER
# ============================================================

def sanitize_dataframe(df: pd.DataFrame) -> pd.DataFrame:

    if df is None:
        return pd.DataFrame()

    if not isinstance(df, pd.DataFrame):

        try:
            df = pd.DataFrame(df)
        except Exception:
            return pd.DataFrame()

    if df.empty:
        return pd.DataFrame()

    try:

        df = _flatten_columns(df)

        df = _fix_datetime_duplicate(df)

        df = _force_ohlc_unique(df)

        df = _remove_duplicate_columns(df)

        df = _sanitize_symbol(df)

        df = _sanitize_numeric(df)

        df = _repair_price_alias(df)

        df = _reset_index_safe(df)

    except Exception:

        logger.exception(
            "[DATAFRAME SANITIZER] unexpected failure"
        )

    return df