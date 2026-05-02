# ============================================================
# File   : utils/symbol_guard.py
# Version: Ver3.0-ULTRA-STABLE-SYMBOL-GUARD
# ------------------------------------------------------------
# ✔ symbol dtype stabilization
# ✔ symbol normalization
# ✔ symbol validation
# ✔ ETF guard support
# ============================================================

from __future__ import annotations

import logging
import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# normalize symbol
# ============================================================

def normalize_symbol(symbol):

    if symbol is None:
        return ""

    try:

        s = str(symbol).strip()

        return s

    except Exception:

        return ""


# ============================================================
# dataframe symbol normalize
# ============================================================

def normalize_symbol_column(df):

    if df is None or df.empty:
        return df

    if "symbol" not in df.columns:
        return df

    try:

        df["symbol"] = df["symbol"].astype(str).str.strip()

    except Exception:

        logger.warning("[SYMBOL GUARD] symbol normalize failed")

    return df


# ============================================================
# validate symbol
# ============================================================

def is_valid_symbol(symbol):

    if symbol is None:
        return False

    s = str(symbol)

    if len(s) < 3:
        return False

    return True


# ============================================================
# filter valid symbols
# ============================================================

def filter_valid_symbols(df):

    if df is None or df.empty:
        return df

    if "symbol" not in df.columns:
        return df

    try:

        df = df[df["symbol"].apply(is_valid_symbol)]

    except Exception:

        logger.warning("[SYMBOL GUARD] filter failed")

    return df