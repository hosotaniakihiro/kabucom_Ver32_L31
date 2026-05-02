# ============================================================
# File   : trading/push/allocator/utils.py
# Version: Ver1.0-PRODUCTION-PUSH-ALLOCATOR-UTILS
# ------------------------------------------------------------
# ✔ symbol extraction utilities
# ✔ DataFrame / list / set safe
# ✔ column alias absorption
# ✔ NaN / inf guard
# ✔ scoring helpers
# ✔ ETF prefix helper
# ✔ production safe
# ============================================================

from __future__ import annotations

import logging
from typing import Iterable, Set, Dict, Any

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# symbol column aliases
# ============================================================

SYMBOL_COLUMNS = (
    "symbol",
    "code",
    "ticker",
    "stock_code",
)


# ============================================================
# symbol column detect
# ============================================================

def detect_symbol_column(df: pd.DataFrame) -> str | None:

    for c in SYMBOL_COLUMNS:
        if c in df.columns:
            return c

    return None


# ============================================================
# extract symbols
# ============================================================

def extract_symbols(data: Any) -> Set[str]:
    """
    DataFrame / list / set / dict / None
    すべてから symbol set を抽出
    """

    if data is None:
        return set()

    # --------------------------------------------------------
    # DataFrame
    # --------------------------------------------------------

    if isinstance(data, pd.DataFrame):

        col = detect_symbol_column(data)

        if col is None:
            logger.warning("[allocator] symbol column not found")
            return set()

        return {
            str(x)
            for x in data[col].dropna().astype(str).tolist()
        }

    # --------------------------------------------------------
    # Series
    # --------------------------------------------------------

    if isinstance(data, pd.Series):

        return {
            str(x)
            for x in data.dropna().astype(str).tolist()
        }

    # --------------------------------------------------------
    # dict
    # --------------------------------------------------------

    if isinstance(data, dict):

        return {str(k) for k in data.keys()}

    # --------------------------------------------------------
    # iterable
    # --------------------------------------------------------

    if isinstance(data, (list, set, tuple)):

        return {str(x) for x in data}

    # --------------------------------------------------------
    # fallback
    # --------------------------------------------------------

    try:
        return {str(x) for x in data}
    except Exception:
        return set()


# ============================================================
# dataframe symbol score
# ============================================================

def dataframe_score_map(
    df: pd.DataFrame,
    score_column: str = "score"
) -> Dict[str, float]:
    """
    DataFrame → symbol score dict
    """

    if df is None or len(df) == 0:
        return {}

    col = detect_symbol_column(df)

    if col is None:
        return {}

    if score_column not in df.columns:
        return {}

    out: Dict[str, float] = {}

    for _, row in df.iterrows():

        sym = str(row[col])

        val = row[score_column]

        if pd.isna(val):
            val = 0.0

        try:
            val = float(val)
        except Exception:
            val = 0.0

        if np.isinf(val):
            val = 0.0

        out[sym] = val

    return out


# ============================================================
# normalize symbol set
# ============================================================

def normalize_symbol_set(symbols: Iterable[str] | None) -> Set[str]:

    if symbols is None:
        return set()

    return {
        str(s)
        for s in symbols
        if s is not None
    }


# ============================================================
# ETF prefix check
# ============================================================

def is_etf(symbol: str, prefixes: tuple[str, ...]) -> bool:

    if not prefixes:
        return False

    s = str(symbol)

    for p in prefixes:
        if s.startswith(p):
            return True

    return False


# ============================================================
# filter ETF
# ============================================================

def filter_etf(symbols: Iterable[str], prefixes: tuple[str, ...]) -> Set[str]:

    if not prefixes:
        return set(symbols)

    return {
        s for s in symbols
        if not is_etf(s, prefixes)
    }


# ============================================================
# merge symbol sets
# ============================================================

def merge_symbol_sets(*sets: Iterable[str]) -> Set[str]:

    out: Set[str] = set()

    for s in sets:

        if s is None:
            continue

        out.update(str(x) for x in s)

    return out


# ============================================================
# safe score get
# ============================================================

def safe_score(score_map: Dict[str, float], symbol: str) -> float:

    v = score_map.get(symbol, 0.0)

    if v is None:
        return 0.0

    try:
        v = float(v)
    except Exception:
        return 0.0

    if np.isnan(v) or np.isinf(v):
        return 0.0

    return v