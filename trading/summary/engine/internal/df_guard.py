# ============================================================
# File   : trading/summary/engine/internal/df_guard.py
# Version: Ver3.0-PRODUCTION-DATAFRAME-GUARD-ULTRA-STABLE
# ------------------------------------------------------------
# ✔ DataFrame完全安定化
# ✔ MultiIndex flatten
# ✔ duplicate列除去
# ✔ symbol dtype統一
# ✔ price / volume alias吸収
# ✔ OHLC fallback生成
# ✔ datetime alias吸収
# ✔ numeric NaN / inf 完全防御
# ✔ dtype安定化
# ✔ pandas alignment crash防止
# ✔ groupby安全保証
# ✔ production safe
# ============================================================

from __future__ import annotations

import logging
import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)


# ============================================================
# alias定義
# ============================================================

PRICE_ALIASES = (
    "price",
    "close",
    "close_price",
    "current_price",
    "CurrentPrice",
    "last_price",
)

VOLUME_ALIASES = (
    "volume",
    "Volume",
    "trade_volume",
    "trading_volume",
)

DATETIME_ALIASES = (
    "datetime",
    "end_time",
    "time",
    "timestamp",
    "snapshot_time",
)


# ============================================================
# MultiIndex flatten
# ============================================================

def _flatten_columns(df: pd.DataFrame) -> pd.DataFrame:

    if isinstance(df.columns, pd.MultiIndex):

        df.columns = [
            "_".join([str(x) for x in col if x not in (None, "")])
            for col in df.columns
        ]

    return df


# ============================================================
# duplicate columns
# ============================================================

def _drop_duplicate_columns(df: pd.DataFrame) -> pd.DataFrame:

    if df.columns.duplicated().any():

        dup = df.columns[df.columns.duplicated()].tolist()

        logger.warning(
            "[DF GUARD] duplicate columns removed: %s",
            dup
        )

        df = df.loc[:, ~df.columns.duplicated()]

    return df


# ============================================================
# index repair
# ============================================================

def _repair_index(df: pd.DataFrame) -> pd.DataFrame:

    try:

        if isinstance(df.index, pd.MultiIndex):
            df = df.reset_index()

        elif df.index.name is not None:
            df = df.reset_index()

    except Exception:

        logger.exception("[DF GUARD] index repair failed")

    return df


# ============================================================
# symbol normalize
# ============================================================

def _ensure_symbol(df: pd.DataFrame) -> pd.DataFrame:

    if "symbol" not in df.columns:

        for alt in ("code", "ticker", "stock_code"):

            if alt in df.columns:

                df["symbol"] = df[alt]

                logger.warning(
                    "[DF GUARD] symbol alias used: %s",
                    alt
                )

                break

    if "symbol" in df.columns:

        try:
            df["symbol"] = df["symbol"].astype(str)
        except Exception:
            pass

    return df


# ============================================================
# price alias
# ============================================================

def _repair_price(df: pd.DataFrame) -> pd.DataFrame:

    if "price" not in df.columns:

        for col in PRICE_ALIASES:

            if col in df.columns:

                df["price"] = df[col]

                logger.warning(
                    "[DF GUARD] price alias used: %s",
                    col
                )

                break

    return df


# ============================================================
# volume alias
# ============================================================

def _repair_volume(df: pd.DataFrame) -> pd.DataFrame:

    if "volume" not in df.columns:

        for col in VOLUME_ALIASES:

            if col in df.columns:

                df["volume"] = df[col]

                logger.warning(
                    "[DF GUARD] volume alias used: %s",
                    col
                )

                break

    if "volume" not in df.columns:
        df["volume"] = 0.0

    return df


# ============================================================
# datetime alias
# ============================================================

def _repair_datetime(df: pd.DataFrame) -> pd.DataFrame:

    if "datetime" not in df.columns:

        for col in DATETIME_ALIASES:

            if col in df.columns:

                df["datetime"] = df[col]

                logger.warning(
                    "[DF GUARD] datetime alias used: %s",
                    col
                )

                break

    if "datetime" in df.columns:

        df["datetime"] = pd.to_datetime(
            df["datetime"],
            errors="coerce"
        )

        before = len(df)

        df = df.dropna(subset=["datetime"])

        dropped = before - len(df)

        if dropped > 0:

            logger.warning(
                "[DF GUARD] dropped invalid datetime rows=%s",
                dropped
            )

    return df


# ============================================================
# OHLC fallback
# ============================================================

def _ensure_ohlc(df: pd.DataFrame) -> pd.DataFrame:

    if "close" in df.columns:

        if "open" not in df.columns:
            df["open"] = df["close"]

        if "high" not in df.columns:
            df["high"] = df["close"]

        if "low" not in df.columns:
            df["low"] = df["close"]

    return df


# ============================================================
# numeric sanitize
# ============================================================

def _sanitize_numeric(df: pd.DataFrame) -> pd.DataFrame:

    try:

        num_cols = df.select_dtypes(include=[np.number]).columns

        for col in num_cols:

            df[col] = (
                df[col]
                .replace([np.inf, -np.inf], np.nan)
                .fillna(0)
                .clip(-1e12, 1e12)
            )

    except Exception:

        logger.exception("[DF GUARD] numeric sanitize failed")

    return df


# ============================================================
# main
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

        df = df.copy()

        df = _flatten_columns(df)
        df = _drop_duplicate_columns(df)
        df = _repair_index(df)

        df = _ensure_symbol(df)

        df = _repair_price(df)
        df = _repair_volume(df)
        df = _repair_datetime(df)

        df = _ensure_ohlc(df)

        df = _sanitize_numeric(df)

        return df

    except Exception:

        logger.exception("[DF GUARD] fatal error")

        return pd.DataFrame()


# ============================================================
# datetime sort
# ============================================================

def safe_datetime(df: pd.DataFrame) -> pd.DataFrame:

    if df.empty:
        return df

    if "datetime" not in df.columns:
        return df

    try:

        df = df.copy()

        df["datetime"] = pd.to_datetime(
            df["datetime"],
            errors="coerce"
        )

        df = df.dropna(subset=["datetime"])

        if "symbol" in df.columns:

            df = df.sort_values(
                ["symbol", "datetime"],
                kind="mergesort"
            )

        else:

            df = df.sort_values("datetime")

        return df

    except Exception:

        logger.exception("[DF GUARD] safe_datetime failed")

        return df


# ============================================================
# duplicate OHLC
# ============================================================

def drop_duplicate_ohlc(df: pd.DataFrame) -> pd.DataFrame:

    if df.empty:
        return df

    if {"symbol", "datetime"}.issubset(df.columns):

        try:

            df = (
                df
                .sort_values(["symbol", "datetime"])
                .drop_duplicates(
                    ["symbol", "datetime"],
                    keep="last"
                )
            )

        except Exception:

            logger.exception("[DF GUARD] duplicate drop failed")

    return df