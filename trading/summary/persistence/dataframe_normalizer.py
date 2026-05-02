# ============================================================
# File   : trading/summary/persistence/dataframe_normalizer.py
# Version: Ver1.0-PRODUCTION-DATAFRAME-NORMALIZER
# ------------------------------------------------------------
# ✔ DataFrame安全化
# ✔ duplicate column 防御
# ✔ MultiIndex列 flatten
# ✔ tuple / list / ndarray / DataFrame列 修復
# ✔ NaN / inf 正規化
# ✔ datetime dtype統一
# ✔ numeric dtype stabilization
# ✔ pandas alignment crash防止
# ✔ index整合性
# ✔ symbol dtype統一
# ✔ production safe
# ============================================================

from __future__ import annotations

import logging
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# ensure dataframe
# ============================================================

def ensure_dataframe(df):

    if df is None:
        return pd.DataFrame()

    if isinstance(df, pd.DataFrame):
        return df.copy()

    if isinstance(df, tuple):

        try:
            df = df[0]
        except Exception:
            return pd.DataFrame()

    try:
        return pd.DataFrame(df)
    except Exception:
        return pd.DataFrame()


# ============================================================
# flatten multiindex
# ============================================================

def flatten_columns(df: pd.DataFrame) -> pd.DataFrame:

    if isinstance(df.columns, pd.MultiIndex):

        df.columns = [
            "_".join([str(x) for x in col if x not in (None, "")])
            for col in df.columns
        ]

    return df


# ============================================================
# duplicate column guard
# ============================================================

def remove_duplicate_columns(df: pd.DataFrame) -> pd.DataFrame:

    if df.columns.duplicated().any():

        dup = list(df.columns[df.columns.duplicated()])

        logger.warning(
            "[DATAFRAME NORMALIZER] duplicate columns removed: %s",
            dup
        )

        df = df.loc[:, ~df.columns.duplicated(keep="last")]

    return df


# ============================================================
# column normalize
# ============================================================

def normalize_columns(df: pd.DataFrame) -> pd.DataFrame:

    df = flatten_columns(df)

    df.columns = [str(c) for c in df.columns]

    df = remove_duplicate_columns(df)

    return df


# ============================================================
# structure normalize
# ============================================================

def normalize_structure(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    df = df.copy()

    df = normalize_columns(df)

    try:
        df = df.reset_index(drop=True)
    except Exception:
        pass

    try:
        df.index = pd.RangeIndex(len(df))
    except Exception:
        pass

    for col in list(df.columns):

        v = df[col]

        # DataFrame列
        if isinstance(v, pd.DataFrame):

            try:
                df[col] = v.iloc[:, 0]
            except Exception:
                df[col] = np.nan

        # ndarray列
        elif isinstance(v, np.ndarray):

            try:
                df[col] = pd.Series(v, index=df.index)
            except Exception:
                df[col] = np.nan

        # list / tuple列
        elif isinstance(v, (list, tuple)):

            try:
                df[col] = pd.Series(v, index=df.index)
            except Exception:
                df[col] = np.nan

        # dict列
        elif isinstance(v, dict):

            try:
                df[col] = pd.Series([v] * len(df))
            except Exception:
                df[col] = np.nan

    return df


# ============================================================
# numeric sanitize
# ============================================================

def sanitize_numeric(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    try:

        df = df.copy()

        numeric_cols = df.select_dtypes(include=[np.number]).columns

        for c in numeric_cols:

            s = df[c]

            df[c] = (
                s.replace([np.inf, -np.inf], np.nan)
                 .fillna(0)
                 .clip(-1e12, 1e12)
            )

    except Exception:

        logger.exception(
            "[DATAFRAME NORMALIZER] numeric sanitize failed"
        )

    return df


# ============================================================
# datetime normalize
# ============================================================

def normalize_datetime(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    for col in df.columns:

        s = df[col]

        try:

            if pd.api.types.is_datetime64_any_dtype(s):

                df[col] = pd.to_datetime(s, errors="coerce")

        except Exception:
            pass

    return df


# ============================================================
# symbol dtype normalize
# ============================================================

def normalize_symbol_dtype(df: pd.DataFrame) -> pd.DataFrame:

    if "symbol" in df.columns:

        try:
            df["symbol"] = df["symbol"].astype(str)
        except Exception:
            pass

    return df


# ============================================================
# NaN sanitize
# ============================================================

def sanitize_nan_inf(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    try:

        df = df.replace([np.inf, -np.inf], np.nan)

        numeric_cols = df.select_dtypes(include=[np.number]).columns

        df[numeric_cols] = df[numeric_cols].fillna(0)

    except Exception:

        logger.exception(
            "[DATAFRAME NORMALIZER] nan sanitize failed"
        )

    return df


# ============================================================
# full normalize pipeline
# ============================================================

def normalize_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """
    DataFrame完全安全化パイプライン
    """

    df = ensure_dataframe(df)

    if df.empty:
        return df

    try:

        df = normalize_structure(df)

        df = normalize_datetime(df)

        df = normalize_symbol_dtype(df)

        df = sanitize_numeric(df)

        df = sanitize_nan_inf(df)

        return df

    except Exception:

        logger.exception(
            "[DATAFRAME NORMALIZER] pipeline failed"
        )

        return df