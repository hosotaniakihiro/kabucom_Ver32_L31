# ============================================================
# File   : utils/df_guard/ohlc_guard.py
# Version: Ver1.0-INSTITUTIONAL-OHLC-GUARD
# ------------------------------------------------------------
# ✔ OHLC alias repair
# ✔ duplicate OHLC guard
# ✔ OHLC欠損補完（price fallback）
# ✔ price列からOHLC生成
# ✔ dtype安全化
# ✔ pandas崩壊防止
# ✔ production safe
# ============================================================

from __future__ import annotations

import logging
import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)


# ============================================================
# OHLC alias repair
# ============================================================

def repair_ohlc_alias(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    try:

        alias = {
            "open_price": "open",
            "high_price": "high",
            "low_price": "low",
            "close_price": "close",
            "Open": "open",
            "High": "high",
            "Low": "low",
            "Close": "close",
        }

        rename_map = {}

        for k, v in alias.items():

            if k in df.columns and v not in df.columns:
                rename_map[k] = v

        if rename_map:

            df = df.rename(columns=rename_map)

            logger.warning(
                "[OHLC GUARD] alias repaired: %s",
                rename_map
            )

    except Exception as e:

        logger.warning(
            "[OHLC GUARD] alias repair failed: %s", e
        )

    return df


# ============================================================
# duplicate OHLC guard
# ============================================================

def ensure_ohlc_unique(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    try:

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
                "[OHLC GUARD] duplicated %s removed",
                c
            )

    except Exception as e:

        logger.warning(
            "[OHLC GUARD] duplicate guard failed: %s", e
        )

    return df


# ============================================================
# ensure numeric OHLC
# ============================================================

def ensure_ohlc_numeric(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    try:

        for col in ["open", "high", "low", "close"]:

            if col in df.columns:

                df[col] = pd.to_numeric(
                    df[col],
                    errors="coerce"
                )

    except Exception as e:

        logger.warning(
            "[OHLC GUARD] numeric conversion failed: %s", e
        )

    return df


# ============================================================
# fill OHLC from price
# ============================================================

def fill_ohlc_from_price(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    if "price" not in df.columns:
        return df

    try:

        for col in ["open", "high", "low", "close"]:

            if col not in df.columns:
                df[col] = df["price"]

            else:
                df[col] = df[col].fillna(df["price"])

        logger.warning(
            "[OHLC GUARD] OHLC filled from price"
        )

    except Exception as e:

        logger.warning(
            "[OHLC GUARD] fill from price failed: %s", e
        )

    return df


# ============================================================
# generate OHLC from price（完全欠損時）
# ============================================================

def generate_ohlc_from_price(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    if "price" not in df.columns:
        return df

    try:

        if not any(c in df.columns for c in ["open", "high", "low", "close"]):

            df["open"] = df["price"]
            df["high"] = df["price"]
            df["low"] = df["price"]
            df["close"] = df["price"]

            logger.warning(
                "[OHLC GUARD] OHLC generated from price"
            )

    except Exception as e:

        logger.warning(
            "[OHLC GUARD] generate failed: %s", e
        )

    return df


# ============================================================
# validate OHLC logic（価格整合性）
# ============================================================

def validate_ohlc_logic(df: pd.DataFrame) -> pd.DataFrame:
    """
    high >= open/close >= low を保証
    """

    if df is None or df.empty:
        return df

    required = {"open", "high", "low", "close"}

    if not required.issubset(df.columns):
        return df

    try:

        df = df.copy()

        df["high"] = df[["open", "high", "close"]].max(axis=1)
        df["low"] = df[["open", "low", "close"]].min(axis=1)

    except Exception as e:

        logger.warning(
            "[OHLC GUARD] validation failed: %s", e
        )

    return df


# ============================================================
# FULL PIPELINE
# ============================================================

def ensure_ohlc(df: pd.DataFrame) -> pd.DataFrame:

    if df is None:
        return pd.DataFrame()

    if df.empty:
        return df

    try:

        df = repair_ohlc_alias(df)

        df = ensure_ohlc_unique(df)

        df = generate_ohlc_from_price(df)

        df = fill_ohlc_from_price(df)

        df = ensure_ohlc_numeric(df)

        df = validate_ohlc_logic(df)

    except Exception as e:

        logger.exception(
            "[OHLC GUARD] ensure_ohlc failed: %s", e
        )

    return df


# ============================================================
# public API
# ============================================================

__all__ = [
    "repair_ohlc_alias",
    "ensure_ohlc_unique",
    "ensure_ohlc_numeric",
    "fill_ohlc_from_price",
    "generate_ohlc_from_price",
    "validate_ohlc_logic",
    "ensure_ohlc",
]