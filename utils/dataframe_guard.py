# ============================================================
# File   : utils/dataframe_guard.py
# Version: Ver6.1-INSTITUTIONAL-ULTRA-STABLE-DATAFRAME-GUARD
# ------------------------------------------------------------
# ✔ dataframe sanitizer
# ✔ duplicate column guard
# ✔ MultiIndex flatten
# ✔ datetime guard
# ✔ datetime index recovery
# ✔ datetime fallback repair
# ✔ symbol dtype stabilization
# ✔ numeric NaN / inf protection
# ✔ extreme value guard
# ✔ OHLC duplicate guard
# ✔ OHLC alias repair
# ✔ datetime duplicate guard
# ✔ index reset safety
# ✔ pandas alignment crash防止
# ✔ duplicate label crash防止
# ✔ timezone safety
# ✔ production safe utilities
# ============================================================

from __future__ import annotations

import logging
import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)


# ============================================================
# flatten multiindex
# ============================================================

def flatten_columns(df: pd.DataFrame) -> pd.DataFrame:

    if df is None:
        return pd.DataFrame()

    if df.empty:
        return df

    if isinstance(df.columns, pd.MultiIndex):

        df = df.copy()

        df.columns = [
            "_".join([str(x) for x in col if x not in (None, "")])
            for col in df.columns.to_flat_index()
        ]

        logger.warning("[DATAFRAME GUARD] MultiIndex columns flattened")

    return df


# ============================================================
# OHLC alias repair
# ============================================================

def repair_ohlc_alias(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    alias = {
        "open_price": "open",
        "high_price": "high",
        "low_price": "low",
        "close_price": "close",
    }

    rename_map = {}

    for k, v in alias.items():

        if k in df.columns and v not in df.columns:
            rename_map[k] = v

    if rename_map:

        df = df.rename(columns=rename_map)

        logger.warning(
            "[DATAFRAME GUARD] OHLC alias repaired: %s",
            rename_map
        )

    return df


# ============================================================
# duplicate column guard
# ============================================================

def remove_duplicate_columns(df: pd.DataFrame) -> pd.DataFrame:

    if df is None:
        return pd.DataFrame()

    if df.empty:
        return df

    if df.columns.duplicated().any():

        dup = df.columns[df.columns.duplicated()].tolist()

        logger.warning(
            "[DATAFRAME GUARD] duplicate columns removed: %s",
            dup
        )

        df = df.loc[:, ~df.columns.duplicated()]

    return df


# ============================================================
# datetime duplicate guard
# ============================================================

def fix_datetime_duplicate(df: pd.DataFrame) -> pd.DataFrame:

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

    df = df.drop(df.columns[drop], axis=1)

    logger.warning("[DATAFRAME GUARD] duplicated datetime columns removed")

    return df


# ============================================================
# OHLC duplicate guard
# ============================================================

def ensure_ohlc_unique(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    cols = ["open", "high", "low", "close"]

    for c in cols:

        idx = [
            i for i, col in enumerate(df.columns)
            if col == c
        ]

        if len(idx) <= 1:
            continue

        drop = idx[1:]

        df = df.drop(df.columns[drop], axis=1)

        logger.warning(
            "[DATAFRAME GUARD] duplicated %s removed",
            c
        )

    return df


# ============================================================
# symbol dtype stabilization
# ============================================================

def stabilize_symbol(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    if "symbol" in df.columns:

        try:
            df["symbol"] = df["symbol"].astype(str).str.strip()
        except Exception:
            pass

    return df


# ============================================================
# datetime index recovery
# ============================================================

def recover_datetime_from_index(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    if "datetime" not in df.columns:

        if df.index.name == "datetime":

            df = df.reset_index()

            logger.warning(
                "[DATAFRAME GUARD] datetime recovered from index"
            )

    return df


# ============================================================
# datetime fallback repair
# ============================================================

def repair_datetime(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    if "datetime" in df.columns:
        return df

    aliases = [
        "end_time",
        "start_time",
        "timestamp",
        "time",
        "t_floor",
    ]

    for col in aliases:

        if col in df.columns:

            df["datetime"] = df[col]

            logger.warning(
                "[DATETIME GUARD] alias used: %s -> datetime",
                col
            )

            return df

    logger.error("[DATETIME GUARD] no time column detected")

    return df


# ============================================================
# datetime normalize
# ============================================================

def ensure_datetime(df: pd.DataFrame) -> pd.DataFrame:

    if df is None:
        return pd.DataFrame()

    if df.empty:
        return df

    df = df.copy()

    df = recover_datetime_from_index(df)

    df = repair_datetime(df)

    if "datetime" not in df.columns:
        return df

    df["datetime"] = pd.to_datetime(
        df["datetime"],
        errors="coerce",
        utc=False
    )

    before = len(df)

    df = df[df["datetime"].notna()]

    dropped = before - len(df)

    if dropped > 0:

        logger.warning(
            "[DATAFRAME GUARD] dropped rows without datetime: %s",
            dropped
        )

    return df


# ============================================================
# numeric sanitize
# ============================================================

def sanitize_numeric(df: pd.DataFrame) -> pd.DataFrame:

    if df is None:
        return pd.DataFrame()

    if df.empty:
        return df

    df = df.copy()

    try:

        numeric_cols = df.select_dtypes(include=[np.number]).columns

        for c in numeric_cols:

            df[c] = (
                df[c]
                .replace([np.inf, -np.inf], np.nan)
            )

    except Exception:

        logger.warning("[DATAFRAME GUARD] numeric sanitize failed")

    return df


# ============================================================
# extreme value guard
# ============================================================

def clip_extreme_values(df: pd.DataFrame, limit=1e12) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    df = df.copy()

    try:

        numeric_cols = df.select_dtypes(include=[np.number]).columns

        for c in numeric_cols:

            df[c] = df[c].clip(-limit, limit)

    except Exception:

        logger.warning("[DATAFRAME GUARD] extreme clipping failed")

    return df


# ============================================================
# duplicate index guard
# ============================================================

def remove_duplicate_index(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    try:

        if df.index.duplicated().any():

            df = df[~df.index.duplicated(keep="last")]

            logger.warning(
                "[DATAFRAME GUARD] duplicated index removed"
            )

    except Exception:
        pass

    return df


# ============================================================
# reset index safe
# ============================================================

def safe_reset_index(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    try:
        return df.reset_index(drop=True)
    except Exception:
        return df


# ============================================================
# FULL SANITIZER
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
        return df

    df = flatten_columns(df)

    df = repair_ohlc_alias(df)

    df = fix_datetime_duplicate(df)

    df = ensure_ohlc_unique(df)

    df = remove_duplicate_columns(df)

    df = stabilize_symbol(df)

    df = ensure_datetime(df)

    df = sanitize_numeric(df)

    df = clip_extreme_values(df)

    df = remove_duplicate_index(df)

    df = safe_reset_index(df)

    return df


# ============================================================
# latest rows extractor
# ============================================================

def extract_latest_by_symbol(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return pd.DataFrame()

    if "symbol" not in df.columns:
        return df

    if "datetime" not in df.columns:
        return df

    return (
        df
        .sort_values(["symbol", "datetime"], kind="mergesort")
        .drop_duplicates("symbol", keep="last")
        .reset_index(drop=True)
    )


# ============================================================
# ensure dataframe
# ============================================================

def ensure_dataframe(df) -> pd.DataFrame:

    if df is None:
        return pd.DataFrame()

    if isinstance(df, pd.DataFrame):
        return df

    try:
        return pd.DataFrame(df)
    except Exception:
        logger.warning("[DATAFRAME GUARD] ensure_dataframe failed")
        return pd.DataFrame()


# ============================================================
# backward compatibility
# ============================================================

def repair_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    return sanitize_dataframe(df)


def sanitize_datetime(df: pd.DataFrame) -> pd.DataFrame:
    return ensure_datetime(df)