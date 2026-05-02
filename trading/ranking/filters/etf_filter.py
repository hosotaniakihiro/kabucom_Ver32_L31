# ============================================================
# File   : trading/ranking/filters/etf_filter.py
# Version: Ver1.0-PRODUCTION-ETF-FILTER
# ------------------------------------------------------------
# ✔ ETF / ETN guard
# ✔ index linked products guard
# ✔ prefix based fast filtering
# ✔ symbol safety
# ✔ pandas vectorized
# ✔ logging
# ✔ extensible blacklist
# ✔ production safe
# ============================================================

from __future__ import annotations

import logging
import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# ETF prefix list (Tokyo exchange ETF / ETN)
# ============================================================

ETF_PREFIX = (
    "130", "131", "132", "133", "134", "135", "136",
    "138", "139",
    "145",
    "147", "148", "149",
    "155",
    "157", "158", "159",
    "165",
    "167", "168", "169",
    "203",
    "204",
    "206",
    "207",
    "208",
    "209"
)


# ============================================================
# explicit blacklist (rare cases)
# ============================================================

ETF_BLACKLIST = {
    "1301",   # TOPIX ETF variants
    "1305",
    "1306",
    "1308",
    "1310",
    "1319",
    "1321",
    "1329",
    "1346",
    "1348",
    "1357",
    "1360",
    "1365",
    "1366",
    "1475",
    "1476",
    "1477",
    "1482",
    "1489",
    "1492",
    "1493",
}


# ============================================================
# symbol normalization
# ============================================================

def _normalize_symbol(df: pd.DataFrame) -> pd.DataFrame:

    if "symbol" not in df.columns:
        return df

    df["symbol"] = (
        df["symbol"]
        .astype(str)
        .str.strip()
    )

    return df


# ============================================================
# prefix filter
# ============================================================

def _prefix_filter(df: pd.DataFrame) -> pd.DataFrame:

    if "symbol" not in df.columns:
        return df

    before = len(df)

    mask = ~df["symbol"].str.startswith(ETF_PREFIX)

    df = df[mask]

    after = len(df)

    if before != after:

        logger.debug(
            "[etf_filter] prefix filter removed %s rows",
            before - after
        )

    return df


# ============================================================
# blacklist filter
# ============================================================

def _blacklist_filter(df: pd.DataFrame) -> pd.DataFrame:

    if "symbol" not in df.columns:
        return df

    before = len(df)

    df = df[~df["symbol"].isin(ETF_BLACKLIST)]

    after = len(df)

    if before != after:

        logger.debug(
            "[etf_filter] blacklist removed %s rows",
            before - after
        )

    return df


# ============================================================
# main API
# ============================================================

def apply_etf_filter(df: pd.DataFrame) -> pd.DataFrame:
    """
    Remove ETF / ETN / index products from dataframe.

    Filtering logic:

    1. prefix filter
    2. explicit blacklist

    """

    if df is None:
        return pd.DataFrame()

    if not isinstance(df, pd.DataFrame):
        return pd.DataFrame()

    if df.empty:
        return df

    try:

        df = df.copy()

        df = _normalize_symbol(df)

        df = _prefix_filter(df)

        if df.empty:
            return df

        df = _blacklist_filter(df)

        return df

    except Exception:

        logger.exception(
            "[etf_filter] failed"
        )

        return pd.DataFrame()