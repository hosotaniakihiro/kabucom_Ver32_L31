# ============================================================
# File   : utils/dataframe_guard.py
# Version: Ver7-PRODUCTION-ULTRA-STABLE-DATAFRAME-GUARD
# ------------------------------------------------------------
# ✔ dataframe sanitizer
# ✔ MultiIndex flatten
# ✔ duplicate column / index guard
# ✔ datetime normalize + fallback
# ✔ symbol normalize
# ✔ numeric NaN / inf protection
# ✔ extreme value guard
# ✔ OHLC alias repair
# ✔ OHLC duplicate guard
# ✔ column auto-add
# ✔ dtype stabilization
# ✔ pandas alignment crash防止
# ✔ timezone safety
# ✔ production hardened
# ============================================================

from __future__ import annotations

import logging
import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)


# ============================================================
# main entry
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

    df = df.copy()

    df = _flatten_columns(df)
    df = _remove_duplicate_columns(df)
    df = _reset_index_safe(df)

    df = _normalize_symbol(df)
    df = _normalize_datetime(df)

    df = _repair_ohlc_alias(df)
    df = _remove_duplicate_rows(df)

    df = _sanitize_numeric(df)
    df = _clip_extreme_values(df)

    return df


# ============================================================
# flatten MultiIndex
# ============================================================

def _flatten_columns(df: pd.DataFrame) -> pd.DataFrame:

    if isinstance(df.columns, pd.MultiIndex):

        df.columns = [
            "_".join([str(c) for c in col if c != ""])
            for col in df.columns
        ]

    return df


# ============================================================
# duplicate columns
# ============================================================

def _remove_duplicate_columns(df: pd.DataFrame) -> pd.DataFrame:

    if df.columns.duplicated().any():

        dup = df.columns[df.columns.duplicated()].tolist()

        logger.warning(
            "[df_guard] duplicate columns removed -> %s",
            dup
        )

        df = df.loc[:, ~df.columns.duplicated()].copy()

    return df


# ============================================================
# index reset
# ============================================================

def _reset_index_safe(df: pd.DataFrame) -> pd.DataFrame:

    try:
        df = df.reset_index(drop=True)
    except Exception:
        pass

    return df


# ============================================================
# symbol normalize
# ============================================================

def _normalize_symbol(df: pd.DataFrame) -> pd.DataFrame:

    if "symbol" in df.columns:

        try:
            df["symbol"] = (
                df["symbol"]
                .astype(str)
                .str.strip()
            )
        except Exception:
            pass

    return df


# ============================================================
# datetime normalize
# ============================================================

def _normalize_datetime(df: pd.DataFrame) -> pd.DataFrame:

    if "datetime" not in df.columns:
        return df

    try:

        df["datetime"] = pd.to_datetime(
            df["datetime"],
            errors="coerce"
        )

        # timezone 제거（統一）
        if hasattr(df["datetime"].dt, "tz"):
            df["datetime"] = df["datetime"].dt.tz_localize(None)

        bad = df["datetime"].isna().sum()

        if bad > 0:

            logger.warning(
                "[df_guard] drop NaT datetime rows -> %s",
                bad
            )

            df = df.dropna(subset=["datetime"])

    except Exception:

        logger.exception("datetime normalize failed")

    return df


# ============================================================
# OHLC alias repair
# ============================================================

def _repair_ohlc_alias(df: pd.DataFrame) -> pd.DataFrame:

    alias_map = {
        "Open": "open",
        "High": "high",
        "Low": "low",
        "Close": "close",
        "Volume": "volume",
        "CurrentPrice": "close",
        "price": "close",
    }

    for src, dst in alias_map.items():

        if src in df.columns and dst not in df.columns:

            df[dst] = df[src]

    return df


# ============================================================
# duplicate rows
# ============================================================

def _remove_duplicate_rows(df: pd.DataFrame) -> pd.DataFrame:

    if {"symbol", "datetime"}.issubset(df.columns):

        before = len(df)

        df = df.drop_duplicates(
            subset=["symbol", "datetime"],
            keep="last"
        )

        after = len(df)

        if before != after:

            logger.warning(
                "[df_guard] duplicate rows removed -> %s",
                before - after
            )

    return df


# ============================================================
# numeric sanitize
# ============================================================

def _sanitize_numeric(df: pd.DataFrame) -> pd.DataFrame:

    try:

        num_cols = df.select_dtypes(include=np.number).columns

        df[num_cols] = (
            df[num_cols]
            .replace([np.inf, -np.inf], np.nan)
            .fillna(0)
        )

    except Exception:

        logger.exception("numeric sanitize failed")

    return df


# ============================================================
# extreme value guard（暴走防止）
# ============================================================

def _clip_extreme_values(df: pd.DataFrame) -> pd.DataFrame:

    try:

        num_cols = df.select_dtypes(include=np.number).columns

        df[num_cols] = df[num_cols].clip(-1e12, 1e12)

    except Exception:

        logger.exception("clip failed")

    return df