# ============================================================
# File   : trading/summary/utils/dataframe_prepare.py
# Version: Ver1.0-PRODUCTION-DATAFRAME-PREPARE-GUARD
# ------------------------------------------------------------
# ✔ dataframe hard guard
# ✔ duplicate column repair
# ✔ MultiIndex flatten
# ✔ tuple/list column repair
# ✔ price alias repair
# ✔ datetime guard
# ✔ dtype stabilization
# ✔ NaN / inf sanitize
# ✔ pandas alignment crash prevention
# ✔ real-time trading safe
# ✔ institutional production module
# ============================================================

from __future__ import annotations

import logging
import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)


# ============================================================
# HARD DATAFRAME GUARD
# ============================================================

def ensure_dataframe(df):

    if df is None:
        return pd.DataFrame()

    if isinstance(df, pd.DataFrame):
        return df

    try:

        if isinstance(df, dict):
            return pd.DataFrame(df)

        if isinstance(df, list):
            return pd.DataFrame(df)

    except Exception:
        pass

    return pd.DataFrame()


# ============================================================
# COLUMN NORMALIZATION
# ============================================================

def normalize_columns(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    try:

        # MultiIndex flatten
        if isinstance(df.columns, pd.MultiIndex):

            df.columns = [
                "_".join([str(x) for x in col if x])
                for col in df.columns
            ]

        # tuple / list columns
        df.columns = [
            "_".join(map(str, c)) if isinstance(c, (list, tuple)) else str(c)
            for c in df.columns
        ]

        # duplicate column removal
        if df.columns.duplicated().any():

            dup = list(df.columns[df.columns.duplicated()])

            logger.warning(
                "[DATAFRAME PREPARE] duplicate columns removed: %s",
                dup
            )

            df = df.loc[:, ~df.columns.duplicated(keep="last")]

    except Exception:

        logger.exception(
            "[DATAFRAME PREPARE] column normalize failed"
        )

    return df


# ============================================================
# PRICE ALIAS REPAIR
# ============================================================

def repair_price_alias(df: pd.DataFrame) -> pd.DataFrame:

    alias_map = {
        "close_price": "close",
        "open_price": "open",
        "high_price": "high",
        "low_price": "low",
    }

    try:

        for src, dst in alias_map.items():

            if src in df.columns and dst not in df.columns:

                df[dst] = df[src]

    except Exception:

        logger.exception(
            "[DATAFRAME PREPARE] price alias repair failed"
        )

    return df


# ============================================================
# DATETIME NORMALIZATION
# ============================================================

def normalize_datetime(df: pd.DataFrame) -> pd.DataFrame:

    if "datetime" not in df.columns:
        return df

    try:

        df["datetime"] = pd.to_datetime(
            df["datetime"],
            errors="coerce"
        )

    except Exception:

        logger.exception(
            "[DATAFRAME PREPARE] datetime normalize failed"
        )

    return df


# ============================================================
# SYMBOL NORMALIZATION
# ============================================================

def normalize_symbol(df: pd.DataFrame) -> pd.DataFrame:

    if "symbol" not in df.columns:
        return df

    try:

        df["symbol"] = df["symbol"].astype(str)

    except Exception:

        logger.exception(
            "[DATAFRAME PREPARE] symbol normalize failed"
        )

    return df


# ============================================================
# SORT SYMBOL TIME
# ============================================================

def sort_symbol_datetime(df: pd.DataFrame) -> pd.DataFrame:

    if {"symbol", "datetime"}.issubset(df.columns):

        try:

            df = df.sort_values(
                ["symbol", "datetime"],
                kind="mergesort"
            )

        except Exception:

            logger.exception(
                "[DATAFRAME PREPARE] sort failed"
            )

    return df


# ============================================================
# NUMERIC SANITIZE
# ============================================================

def sanitize_numeric(df: pd.DataFrame) -> pd.DataFrame:

    try:

        df = df.replace([np.inf, -np.inf], np.nan)

        numeric_cols = df.select_dtypes(
            include=[np.number]
        ).columns

        df[numeric_cols] = df[numeric_cols].fillna(0)

    except Exception:

        logger.exception(
            "[DATAFRAME PREPARE] numeric sanitize failed"
        )

    return df


# ============================================================
# INDEX RESET (alignment crash prevention)
# ============================================================

def reset_index_safe(df: pd.DataFrame) -> pd.DataFrame:

    try:

        df = df.reset_index(drop=True)

    except Exception:

        logger.exception(
            "[DATAFRAME PREPARE] index reset failed"
        )

    return df


# ============================================================
# FULL DATAFRAME PREPARE PIPELINE
# ============================================================

def prepare_dataframe(df: pd.DataFrame) -> pd.DataFrame:

    df = ensure_dataframe(df)

    if df.empty:
        return df

    try:

        df = normalize_columns(df)

        df = repair_price_alias(df)

        df = normalize_datetime(df)

        df = normalize_symbol(df)

        df = sort_symbol_datetime(df)

        df = sanitize_numeric(df)

        df = reset_index_safe(df)

        # duplicate columns 再チェック
        df = normalize_columns(df)

    except Exception:

        logger.exception(
            "[DATAFRAME PREPARE] pipeline failed"
        )

    return df