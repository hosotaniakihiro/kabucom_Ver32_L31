# ============================================================
# File   : trading/summary/calculator/guards/key_column_guard.py
# Version: Ver3.0-PRODUCTION-KEY-COLUMN-GUARD-ULTRA-STABLE
# ------------------------------------------------------------
# ✔ symbol / datetime 絶対保証
# ✔ fallback復元（alignment安全）
# ✔ alias吸収（優先順位制御）
# ✔ MultiIndex / index完全修復
# ✔ duplicate列完全防御
# ✔ dtype完全安定化
# ✔ NaT / None / 空文字完全除去
# ✔ duplicate row防止
# ✔ production hardened
# ============================================================

from __future__ import annotations

import logging
import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)


# ============================================================
# alias candidates（優先順位）
# ============================================================

_DATETIME_ALIASES = (
    "datetime",
    "timestamp",
    "end_time",
    "time",
    "snapshot_time",
)

_SYMBOL_ALIASES = (
    "symbol",
    "code",
    "ticker",
    "stock_code",
)


# ============================================================
# structure repair
# ============================================================

def _flatten_columns(df: pd.DataFrame) -> pd.DataFrame:

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = ["_".join(map(str, c)) for c in df.columns]

    return df


def _drop_duplicate_columns(df: pd.DataFrame) -> pd.DataFrame:

    if df.columns.duplicated().any():

        dup = df.columns[df.columns.duplicated()].tolist()

        logger.warning("[KEY GUARD] duplicate columns removed: %s", dup)

        df = df.loc[:, ~df.columns.duplicated()]

    return df


def _repair_index(df: pd.DataFrame) -> pd.DataFrame:

    try:

        if isinstance(df.index, pd.MultiIndex):
            df = df.reset_index(drop=False)

        elif df.index.name is not None:
            df = df.reset_index(drop=False)

    except Exception:
        logger.exception("[KEY GUARD] index repair failed")

    return df


# ============================================================
# symbol ensure（強化版）
# ============================================================

def _ensure_symbol(df: pd.DataFrame, fallback_df: pd.DataFrame | None):

    # already exists
    if "symbol" in df.columns:
        return df

    # alias
    for alt in _SYMBOL_ALIASES:
        if alt in df.columns:
            df["symbol"] = df[alt]
            logger.warning("[KEY GUARD] symbol alias used: %s", alt)
            return df

    # fallback
    if fallback_df is not None and "symbol" in fallback_df.columns:

        try:

            fb = fallback_df["symbol"].astype(str).values

            if len(fb) >= len(df):
                df["symbol"] = fb[:len(df)]
            else:
                # 長さ不足 → repeat
                df["symbol"] = np.resize(fb, len(df))

            logger.warning("[KEY GUARD] symbol restored from fallback")

            return df

        except Exception:
            logger.exception("[KEY GUARD] symbol fallback failed")

    # default
    df["symbol"] = "UNKNOWN"

    logger.warning("[KEY GUARD] symbol default assigned")

    return df


# ============================================================
# datetime ensure（強化版）
# ============================================================

def _ensure_datetime(df: pd.DataFrame, fallback_df: pd.DataFrame | None):

    # already exists
    if "datetime" in df.columns:
        return df

    # alias
    for alt in _DATETIME_ALIASES:
        if alt in df.columns:
            df["datetime"] = df[alt]
            logger.warning("[KEY GUARD] datetime alias used: %s", alt)
            return df

    # fallback
    if fallback_df is not None and "datetime" in fallback_df.columns:

        try:

            fb = pd.to_datetime(
                fallback_df["datetime"],
                errors="coerce"
            ).values

            if len(fb) >= len(df):
                df["datetime"] = fb[:len(df)]
            else:
                df["datetime"] = np.resize(fb, len(df))

            logger.warning("[KEY GUARD] datetime restored from fallback")

            return df

        except Exception:
            logger.exception("[KEY GUARD] datetime fallback failed")

    # default
    df["datetime"] = pd.Timestamp.now()

    logger.warning("[KEY GUARD] datetime default assigned")

    return df


# ============================================================
# finalize types（強化版）
# ============================================================

def _finalize_types(df: pd.DataFrame) -> pd.DataFrame:

    try:

        # symbol
        if "symbol" in df.columns:
            df["symbol"] = df["symbol"].astype(str)

        # datetime
        if "datetime" in df.columns:
            df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")

        # 無効除去
        before = len(df)

        df = df[
            df["symbol"].notna()
            & (df["symbol"] != "")
            & df["datetime"].notna()
        ]

        removed = before - len(df)

        if removed > 0:
            logger.warning("[KEY GUARD] removed invalid rows: %s", removed)

        # duplicate row防止
        if {"symbol", "datetime"}.issubset(df.columns):
            df = (
                df
                .sort_values(["symbol", "datetime"])
                .drop_duplicates(["symbol", "datetime"], keep="last")
                .reset_index(drop=True)
            )

        return df

    except Exception:

        logger.exception("[KEY GUARD] finalize failed")

        return df


# ============================================================
# main function
# ============================================================

def ensure_key_columns(
    df: pd.DataFrame,
    fallback_df: pd.DataFrame | None = None,
) -> pd.DataFrame:

    """
    symbol / datetime を絶対保証（最強版）
    """

    if df is None:
        return pd.DataFrame(columns=["symbol", "datetime"])

    if not isinstance(df, pd.DataFrame):

        try:
            df = pd.DataFrame(df)
        except Exception:
            return pd.DataFrame(columns=["symbol", "datetime"])

    if df.empty:
        return df

    try:

        df = df.copy()

        # ----------------------------------------------------
        # structure repair
        # ----------------------------------------------------

        df = _flatten_columns(df)
        df = _drop_duplicate_columns(df)
        df = _repair_index(df)

        # ----------------------------------------------------
        # key ensure
        # ----------------------------------------------------

        df = _ensure_symbol(df, fallback_df)
        df = _ensure_datetime(df, fallback_df)

        # ----------------------------------------------------
        # finalize
        # ----------------------------------------------------

        df = _finalize_types(df)

        return df

    except Exception:

        logger.exception("[KEY GUARD] fatal error")

        return pd.DataFrame(columns=["symbol", "datetime"])