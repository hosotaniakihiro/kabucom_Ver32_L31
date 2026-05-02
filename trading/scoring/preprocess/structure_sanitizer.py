# ============================================================
# File   : trading/scoring/preprocess/structure_sanitizer.py
# Version: Ver2.0-PRODUCTION-STRUCTURE-SANITIZER-ULTRA-STABLE
# ------------------------------------------------------------
# ✔ DataFrame構造修復
# ✔ tuple / list → DataFrame repair
# ✔ nested dataframe column修復
# ✔ duplicate column完全防御
# ✔ OHLC duplicate guard
# ✔ price alias repair
# ✔ symbol dtype normalize
# ✔ datetime normalize
# ✔ timezone remove
# ✔ pandas alignment crash防止
# ✔ sort normalize
# ✔ index normalize
# ✔ production ultra stability
# ============================================================

from __future__ import annotations

import logging
import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# ensure dataframe
# ============================================================

def _ensure_dataframe(df):

    if df is None:
        return pd.DataFrame()

    if isinstance(df, pd.DataFrame):
        return df

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
# repair nested dataframe column
# ============================================================

def _repair_nested_columns(df: pd.DataFrame) -> pd.DataFrame:

    try:

        for c in df.columns:

            if isinstance(df[c], pd.DataFrame):

                df[c] = df[c].iloc[:, 0]

    except Exception:
        pass

    return df


# ============================================================
# flatten multiindex columns
# ============================================================

def _flatten_columns(df: pd.DataFrame) -> pd.DataFrame:

    if isinstance(df.columns, pd.MultiIndex):

        try:

            df.columns = [
                "_".join(
                    [str(x) for x in col if x not in (None, "")]
                )
                for col in df.columns
            ]

        except Exception:

            df.columns = [str(col) for col in df.columns]

    return df


# ============================================================
# remove duplicate columns
# ============================================================

def _remove_duplicate_columns(df: pd.DataFrame) -> pd.DataFrame:

    try:

        if df.columns.duplicated().any():

            dup = df.columns[df.columns.duplicated()].tolist()

            logger.warning(
                "[STRUCTURE SANITIZER] duplicate columns removed: %s",
                dup,
            )

            df = df.loc[:, ~df.columns.duplicated()].copy()

    except Exception:
        pass

    return df


# ============================================================
# OHLC duplicate guard
# ============================================================

def _force_ohlc_unique(df: pd.DataFrame) -> pd.DataFrame:

    try:

        for col in ["open", "high", "low", "close"]:

            idx = [
                i for i, c in enumerate(df.columns)
                if c == col
            ]

            if len(idx) <= 1:
                continue

            logger.warning(
                "[STRUCTURE SANITIZER] duplicate OHLC column removed: %s",
                col,
            )

            df = df.drop(df.columns[idx[1:]], axis=1)

    except Exception:
        pass

    return df


# ============================================================
# price alias repair
# ============================================================

def _repair_price_columns(df: pd.DataFrame) -> pd.DataFrame:

    mapping = {
        "close_price": "close",
        "open_price": "open",
        "high_price": "high",
        "low_price": "low",
    }

    for src, dst in mapping.items():

        if src in df.columns and dst not in df.columns:

            try:
                df[dst] = df[src]
            except Exception:
                pass

    return df


# ============================================================
# normalize symbol
# ============================================================

def _normalize_symbol(df: pd.DataFrame) -> pd.DataFrame:

    if "symbol" not in df.columns:
        return df

    try:
        df["symbol"] = df["symbol"].astype(str)
    except Exception:
        pass

    return df


# ============================================================
# normalize datetime
# ============================================================

def _normalize_datetime(df: pd.DataFrame) -> pd.DataFrame:

    if "datetime" not in df.columns:
        return df

    try:

        df["datetime"] = pd.to_datetime(
            df["datetime"],
            errors="coerce"
        )

        # timezone remove
        try:
            df["datetime"] = df["datetime"].dt.tz_localize(None)
        except Exception:
            pass

        df = df.dropna(subset=["datetime"])

    except Exception:
        pass

    return df


# ============================================================
# sort normalize
# ============================================================

def _normalize_sort(df: pd.DataFrame) -> pd.DataFrame:

    try:

        if "symbol" in df.columns and "datetime" in df.columns:

            df = df.sort_values(
                ["symbol", "datetime"],
                kind="mergesort"
            )

        elif "datetime" in df.columns:

            df = df.sort_values(
                ["datetime"],
                kind="mergesort"
            )

    except Exception:
        pass

    return df


# ============================================================
# index normalize
# ============================================================

def _normalize_index(df: pd.DataFrame) -> pd.DataFrame:

    try:
        df = df.reset_index(drop=True)
    except Exception:
        pass

    return df


# ============================================================
# main sanitizer
# ============================================================

def sanitize_structure(df: pd.DataFrame) -> pd.DataFrame:
    """
    DataFrame構造修復

    - ensure dataframe
    - repair nested dataframe columns
    - flatten multiindex columns
    - duplicate column 제거
    - OHLC duplicate 제거
    - price alias repair
    - symbol normalize
    - datetime normalize
    - sort normalize
    - index normalize
    """

    df = _ensure_dataframe(df)

    if df.empty:
        return df

    try:

        df = df.copy()

        df = _repair_nested_columns(df)

        df = _flatten_columns(df)

        df = _remove_duplicate_columns(df)

        df = _force_ohlc_unique(df)

        df = _repair_price_columns(df)

        df = _normalize_symbol(df)

        df = _normalize_datetime(df)

        df = _normalize_sort(df)

        df = _normalize_index(df)

        return df

    except Exception:

        logger.exception(
            "[STRUCTURE SANITIZER] structure repair failed"
        )

        return df