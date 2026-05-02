# ============================================================
# File   : trading/ranking/core/column_resolver.py
# Version: Ver3-PRODUCTION-ULTRA-STABLE-COLUMN-RESOLVER
# ------------------------------------------------------------
# ✔ column alias統一
# ✔ 大文字小文字吸収
# ✔ 外部API差異対応（kabu/yahoo等）
# ✔ price / volume / datetime / symbol統一
# ✔ duplicate column guard
# ✔ fallback安全
# ✔ pandas crash防止
# ✔ production hardened
# ============================================================

from __future__ import annotations

import logging
import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# alias定義（ここが重要）
# ============================================================

COLUMN_ALIASES = {
    # --- symbol ---
    "symbol": ["symbol", "code", "ticker"],

    # --- datetime ---
    "datetime": ["datetime", "date", "time", "timestamp"],

    # --- price ---
    "open": ["open", "Open"],
    "high": ["high", "High"],
    "low": ["low", "Low"],
    "close": ["close", "Close", "price", "CurrentPrice"],

    # --- volume ---
    "volume": ["volume", "Volume", "vol"],

    # --- VWAP ---
    "vwap": ["vwap", "VWAP"],

    # --- ranking系 ---
    "best_rank": ["best_rank"],
    "avg_rank": ["avg_rank"],
    "ranking_count": ["ranking_count"],

    # --- 名前 ---
    "symbolname": ["symbolname", "name"],
}


# ============================================================
# helpers
# ============================================================

def _lower_map(df: pd.DataFrame):
    """
    元カラム → lowerカラム の対応辞書
    """
    return {c.lower(): c for c in df.columns}


def _resolve_one(df: pd.DataFrame, target: str, aliases: list[str]):

    lower_map = _lower_map(df)

    for alias in aliases:

        key = alias.lower()

        if key in lower_map:

            src = lower_map[key]

            if src != target:

                if target not in df.columns:
                    df[target] = df[src]

                else:
                    # 既に存在する場合は欠損補完
                    df[target] = df[target].fillna(df[src])

            return df

    return df


def _remove_duplicate_columns(df: pd.DataFrame) -> pd.DataFrame:

    if df.columns.duplicated().any():

        dup = df.columns[df.columns.duplicated()].tolist()

        logger.warning(
            "[column_resolver] duplicate columns removed -> %s",
            dup
        )

        df = df.loc[:, ~df.columns.duplicated()].copy()

    return df


# ============================================================
# main
# ============================================================

def resolve_columns(df: pd.DataFrame) -> pd.DataFrame:
    """
    カラム名の統一

    - alias解決
    - 欠損補完
    - 重複排除
    """

    if df is None:
        return pd.DataFrame()

    if not isinstance(df, pd.DataFrame):
        return pd.DataFrame()

    if df.empty:
        return df

    try:

        df = df.copy()

        # ----------------------------------------------------
        # すべて小文字トリム（安全）
        # ----------------------------------------------------

        df.columns = [
            str(c).strip()
            for c in df.columns
        ]

        # ----------------------------------------------------
        # alias解決
        # ----------------------------------------------------

        for target, aliases in COLUMN_ALIASES.items():
            df = _resolve_one(df, target, aliases)

        # ----------------------------------------------------
        # datetime特殊処理
        # ----------------------------------------------------

        if "datetime" in df.columns:

            try:
                df["datetime"] = pd.to_datetime(
                    df["datetime"],
                    errors="coerce"
                )
            except Exception:
                logger.exception(
                    "[column_resolver] datetime conversion failed"
                )

        # ----------------------------------------------------
        # symbol強制str
        # ----------------------------------------------------

        if "symbol" in df.columns:

            try:
                df["symbol"] = (
                    df["symbol"]
                    .astype(str)
                    .str.strip()
                )
            except Exception:
                pass

        # ----------------------------------------------------
        # duplicate column除去
        # ----------------------------------------------------

        df = _remove_duplicate_columns(df)

        return df

    except Exception:

        logger.exception(
            "[column_resolver] failed"
        )

        return pd.DataFrame()


# ============================================================
# utility（単体解決）
# ============================================================

def resolve_price(df: pd.DataFrame):

    df = resolve_columns(df)

    if "close" in df.columns:
        return df["close"]

    return pd.Series(0, index=df.index)


def resolve_volume(df: pd.DataFrame):

    df = resolve_columns(df)

    if "volume" in df.columns:
        return df["volume"]

    return pd.Series(0, index=df.index)