# ============================================================
# File   : trading/summary/utils/ohlc_guard.py
# Version: Ver1.0-PRODUCTION-OHLC-GUARD
# ------------------------------------------------------------
# ✔ OHLC column guarantee
# ✔ open_price / close_price alias repair
# ✔ duplicate OHLC column guard
# ✔ numeric dtype enforcement
# ✔ NaN / inf protection
# ✔ high/low logical correction
# ✔ extreme value protection
# ✔ pandas alignment crash防止
# ✔ production logging
# ============================================================

from __future__ import annotations

import logging
import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)


# ============================================================
# REMOVE DUPLICATE OHLC COLUMNS
# ============================================================

def _remove_duplicate_ohlc(df: pd.DataFrame) -> pd.DataFrame:

    for c in ["open", "high", "low", "close"]:

        idx = [i for i, col in enumerate(df.columns) if col == c]

        if len(idx) <= 1:
            continue

        logger.warning(
            "[OHLC GUARD] duplicate column removed: %s",
            c
        )

        df = df.drop(df.columns[idx[1:]], axis=1)

    return df


# ============================================================
# PRICE ALIAS REPAIR
# ============================================================

def _repair_price_alias(df: pd.DataFrame) -> pd.DataFrame:

    alias = {
        "open_price": "open",
        "high_price": "high",
        "low_price": "low",
        "close_price": "close",
    }

    for src, dst in alias.items():

        if src in df.columns and dst not in df.columns:

            try:

                df[dst] = df[src]

                logger.info(
                    "[OHLC GUARD] alias repaired: %s → %s",
                    src,
                    dst
                )

            except Exception:

                logger.warning(
                    "[OHLC GUARD] alias repair failed: %s",
                    src
                )

    return df


# ============================================================
# ENSURE OHLC COLUMNS
# ============================================================

def _ensure_ohlc_columns(df: pd.DataFrame) -> pd.DataFrame:

    for c in ["open", "high", "low", "close"]:

        if c not in df.columns:

            logger.warning(
                "[OHLC GUARD] missing column created: %s",
                c
            )

            df[c] = 0.0

    return df


# ============================================================
# NUMERIC CONVERT
# ============================================================

def _convert_numeric(df: pd.DataFrame) -> pd.DataFrame:

    for c in ["open", "high", "low", "close"]:

        try:

            df[c] = pd.to_numeric(df[c], errors="coerce")

        except Exception:

            logger.warning(
                "[OHLC GUARD] numeric conversion failed: %s",
                c
            )

    return df


# ============================================================
# NAN / INF PROTECTION
# ============================================================

def _sanitize_numeric(df: pd.DataFrame) -> pd.DataFrame:

    cols = ["open", "high", "low", "close"]

    df[cols] = (
        df[cols]
        .replace([np.inf, -np.inf], np.nan)
        .fillna(0)
        .clip(-1e9, 1e9)
    )

    return df


# ============================================================
# LOGICAL CORRECTION
# ============================================================

def _fix_price_logic(df: pd.DataFrame) -> pd.DataFrame:

    try:

        # high must be >= others
        df["high"] = df[["high", "open", "close"]].max(axis=1)

        # low must be <= others
        df["low"] = df[["low", "open", "close"]].min(axis=1)

    except Exception:

        logger.warning(
            "[OHLC GUARD] price logic correction failed"
        )

    return df


# ============================================================
# EXTREME VALUE PROTECTION
# ============================================================

def _clip_extreme_prices(df: pd.DataFrame) -> pd.DataFrame:

    try:

        cols = ["open", "high", "low", "close"]

        df[cols] = df[cols].clip(0, 1e9)

    except Exception:

        logger.warning(
            "[OHLC GUARD] extreme value clip failed"
        )

    return df


# ============================================================
# MAIN GUARD
# ============================================================

def guard_ohlc(df: pd.DataFrame) -> pd.DataFrame:

    if df is None:
        return pd.DataFrame()

    if not isinstance(df, pd.DataFrame):

        try:
            df = pd.DataFrame(df)
        except Exception:
            return pd.DataFrame()

    if df.empty:
        return df

    try:

        df = _repair_price_alias(df)

        df = _remove_duplicate_ohlc(df)

        df = _ensure_ohlc_columns(df)

        df = _convert_numeric(df)

        df = _sanitize_numeric(df)

        df = _fix_price_logic(df)

        df = _clip_extreme_prices(df)

    except Exception:

        logger.exception(
            "[OHLC GUARD] unexpected failure"
        )

    return df