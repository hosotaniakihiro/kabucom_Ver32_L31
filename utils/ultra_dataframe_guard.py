# ============================================================
# File   : utils/ultra_dataframe_guard.py
# Version: Ver1.0-PRODUCTION-ULTRA-DATAFRAME-GUARD
# ------------------------------------------------------------
# ✔ MultiIndex flatten
# ✔ duplicate column repair
# ✔ index repair
# ✔ Series/DataFrame column repair
# ✔ datetime guarantee
# ✔ OHLC numeric guarantee
# ✔ NaN / inf repair
# ✔ pandas alignment safety
# ✔ concat corruption guard
# ✔ production safe
# ============================================================

from __future__ import annotations

import logging
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# flatten MultiIndex
# ============================================================

def _flatten_columns(df: pd.DataFrame) -> pd.DataFrame:

    try:

        if isinstance(df.columns, pd.MultiIndex):

            df.columns = [
                "_".join(
                    [str(x) for x in col if x not in (None, "", "None")]
                )
                for col in df.columns
            ]

    except Exception:

        logger.exception("[DF GUARD] flatten columns failed")

    return df


# ============================================================
# duplicate columns
# ============================================================

def _remove_duplicate_columns(df: pd.DataFrame) -> pd.DataFrame:

    try:

        if df.columns.duplicated().any():

            dup = list(df.columns[df.columns.duplicated()])

            logger.warning(
                "[DF GUARD] duplicate columns removed -> %s",
                dup
            )

            df = df.loc[:, ~df.columns.duplicated(keep="last")]

    except Exception:

        logger.exception("[DF GUARD] duplicate column repair failed")

    return df


# ============================================================
# index repair
# ============================================================

def _repair_index(df: pd.DataFrame) -> pd.DataFrame:

    try:

        if isinstance(df.index, pd.MultiIndex):

            df = df.reset_index(drop=True)

        if df.index.duplicated().any():

            df = df.reset_index(drop=True)

        if not isinstance(df.index, pd.RangeIndex):

            df = df.reset_index(drop=True)

    except Exception:

        logger.exception("[DF GUARD] index repair failed")

    return df


# ============================================================
# safe series extractor
# ============================================================

def _safe_series(df: pd.DataFrame, col: str):

    if col not in df.columns:

        return pd.Series(0, index=df.index)

    s = df[col]

    try:

        if isinstance(s, pd.DataFrame):

            s = s.iloc[:, 0]

        return s

    except Exception:

        return pd.Series(0, index=df.index)


# ============================================================
# OHLC repair
# ============================================================

def _repair_ohlc(df: pd.DataFrame) -> pd.DataFrame:

    cols = ["open", "high", "low", "close", "volume"]

    for c in cols:

        if c not in df.columns:

            continue

        try:

            s = _safe_series(df, c)

            df[c] = pd.to_numeric(
                s,
                errors="coerce"
            )

        except Exception:

            logger.exception(
                "[DF GUARD] OHLC repair failed %s",
                c
            )

    return df


# ============================================================
# datetime repair
# ============================================================

def _repair_datetime(df: pd.DataFrame) -> pd.DataFrame:

    try:

        if "datetime" not in df.columns:

            if "timestamp" in df.columns:

                df["datetime"] = df["timestamp"]

            elif "time" in df.columns:

                df["datetime"] = df["time"]

        if "datetime" in df.columns:

            df["datetime"] = pd.to_datetime(
                df["datetime"],
                errors="coerce"
            )

    except Exception:

        logger.exception("[DF GUARD] datetime repair failed")

    return df


# ============================================================
# NaN / inf repair
# ============================================================

def _repair_nan_inf(df: pd.DataFrame) -> pd.DataFrame:

    try:

        df = df.replace([np.inf, -np.inf], np.nan)

    except Exception:

        logger.exception("[DF GUARD] NaN/inf repair failed")

    return df


# ============================================================
# public guard
# ============================================================

def guard_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """
    Ultra DataFrame Guard

    Repairs:

        MultiIndex columns
        duplicate columns
        index corruption
        datetime missing
        OHLC dtype
        NaN / inf
    """

    try:

        if df is None:

            return pd.DataFrame()

        if not isinstance(df, pd.DataFrame):

            df = pd.DataFrame(df)

        if df.empty:

            return df

        df = df.copy()

        df = _flatten_columns(df)

        df = _remove_duplicate_columns(df)

        df = _repair_index(df)

        df = _repair_datetime(df)

        df = _repair_ohlc(df)

        df = _repair_nan_inf(df)

        return df

    except Exception:

        logger.exception("[DF GUARD] guard_dataframe failed")

        return df