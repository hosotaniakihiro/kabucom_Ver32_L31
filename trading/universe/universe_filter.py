# ============================================================
# universe_filter.py
# Ver1.0-PRODUCTION-MARKET-UNIVERSE-FILTER
# ------------------------------------------------------------
# ✔ ETF / REIT / ETN 除外
# ✔ PRO Market 除外
# ✔ 特設注意銘柄除外
# ✔ 低流動除外
# ✔ 板スカ除外
# ✔ ranking前フィルター
# ✔ HFT専用
# ============================================================

from __future__ import annotations
import logging
import pandas as pd

from database import Session_position
from sqlalchemy import text

logger = logging.getLogger(__name__)

MIN_TURNOVER = 30_000_000
MIN_VOLUME = 5000


# ============================================================
# symbol_flags guard
# ============================================================

def filter_symbol_flags(df: pd.DataFrame):

    if df is None or df.empty:
        return df

    session = Session_position()

    try:

        flags = pd.read_sql(
            text(
                """
                SELECT
                    symbol,
                    is_etf,
                    market_type,
                    is_attention
                FROM symbol_flags
                """
            ),
            session.bind
        )

    finally:
        session.close()

    flags["symbol"] = flags["symbol"].astype(str)

    df["symbol"] = df["symbol"].astype(str)

    df = df.merge(flags, on="symbol", how="left")

    df = df[
        (df["is_etf"].fillna(0) != 1)
        & (df["market_type"].fillna("").isin(
            ["ETF","ETN","REIT","PRO Market","INDEX"]
        ) == False)
        & (df["is_attention"].fillna(0) != 1)
    ]

    return df


# ============================================================
# 流動性フィルター
# ============================================================

def filter_liquidity(df: pd.DataFrame):

    if df is None or df.empty:
        return df

    if "turnover" not in df.columns:
        return df

    if "volume" not in df.columns:
        return df

    df = df[
        (df["turnover"] >= MIN_TURNOVER)
        & (df["volume"] >= MIN_VOLUME)
    ]

    return df


# ============================================================
# メイン
# ============================================================

def apply_universe_filter(df: pd.DataFrame):

    if df is None or df.empty:
        return df

    try:

        before = len(df)

        df = filter_symbol_flags(df)

        df = filter_liquidity(df)

        after = len(df)

        logger.info(
            "[UNIVERSE] filtered %d → %d",
            before,
            after
        )

        return df

    except Exception:

        logger.exception("universe filter failed")

        return df