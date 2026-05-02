# ============================================================
# File   : utils/market_filter.py
# Version: Ver1.3-PRODUCTION-CACHED-STABLE
# ------------------------------------------------------------
# ✔ Ver1.2 完全保持（削除ゼロ）
# ✔ DBキャッシュ追加（最重要）
# ✔ NAS負荷激減
# ✔ symbol型安定化
# ✔ 高速filter
# ✔ 本番最適化
# ============================================================

from __future__ import annotations

import logging
import sqlite3
from typing import List, Set

import pandas as pd

logger = logging.getLogger(__name__)

# ============================================================
# DB PATH
# ============================================================

DB_PATH = r"\\192.168.0.22\AutoStockBuyAndSell\Basic\symbol_flags.db"

# ============================================================
# SQL
# ============================================================

SQL_GET_SYMBOLS = """
SELECT symbol
FROM symbol_flags
WHERE market_type IN ('プライム','スタンダード','グロース')
"""

# ============================================================
# CACHE（追加）
# ============================================================

_SYMBOL_CACHE: Set[str] | None = None


def refresh_symbol_cache():
    """手動キャッシュ更新"""
    global _SYMBOL_CACHE
    _SYMBOL_CACHE = None


# ============================================================
# LOAD
# ============================================================

def get_tradeable_symbols() -> List[str]:

    global _SYMBOL_CACHE

    # ★ キャッシュ使用（最重要）
    if _SYMBOL_CACHE is not None:
        return list(_SYMBOL_CACHE)

    conn = None

    try:

        conn = sqlite3.connect(DB_PATH)

        df = pd.read_sql(SQL_GET_SYMBOLS, conn)

        if df.empty:

            logger.warning(
                "[MARKET FILTER] symbol list empty"
            )

            _SYMBOL_CACHE = set()
            return []

        # symbol → str（重要）
        df["symbol"] = df["symbol"].astype(str)

        # duplicate guard
        df = df.drop_duplicates("symbol")

        symbols = set(df["symbol"])

        # ★ キャッシュ保存
        _SYMBOL_CACHE = symbols

        logger.info(
            "[MARKET FILTER] loaded symbols=%s",
            len(symbols),
        )

        return list(symbols)

    except Exception:

        logger.exception(
            "[MARKET FILTER] load failed"
        )

        _SYMBOL_CACHE = set()
        return []

    finally:

        try:
            if conn:
                conn.close()
        except Exception:
            pass


# ============================================================
# dataframe filter
# ============================================================

def filter_tradeable_dataframe(df: pd.DataFrame) -> pd.DataFrame:

    try:

        if df is None:
            return df

        if df.empty:
            return df

        if "symbol" not in df.columns:

            logger.warning(
                "[MARKET FILTER] symbol column missing"
            )

            return df

        # ★ キャッシュ利用
        valid_symbols = _SYMBOL_CACHE or set(get_tradeable_symbols())

        if not valid_symbols:
            logger.warning("[MARKET FILTER] empty cache (skip)")
            return df

        # ★ 高速化（重要）
        sym = df["symbol"].astype(str)

        mask = sym.isin(valid_symbols)

        filtered = df.loc[mask]

        removed = len(df) - len(filtered)

        if removed > 0:

            logger.info(
                "[MARKET FILTER] removed=%s",
                removed,
            )

        return filtered

    except Exception:

        logger.exception(
            "[MARKET FILTER] dataframe filter failed"
        )

        return df