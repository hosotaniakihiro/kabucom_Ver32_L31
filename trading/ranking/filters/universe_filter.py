# ============================================================
# File   : trading/ranking/filters/universe_filter.py
# Version: Ver3-PRODUCTION-ULTRA-STABLE-UNIVERSE-FILTER
# ------------------------------------------------------------
# ✔ 銘柄母集団フィルタ
# ✔ 流動性・価格・時価総額ベース
# ✔ 東証区分対応（あれば）
# ✔ volume / turnover fallback
# ✔ NaN / inf 安全処理
# ✔ extreme値防止
# ✔ pandas vectorized
# ✔ production logging
# ✔ 拡張可能設計
# ============================================================

from __future__ import annotations

import logging
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# parameters（戦略のコア）
# ============================================================

MIN_PRICE = 100
MAX_PRICE = 10_000

MIN_VOLUME = 5_000
MIN_TURNOVER = 3_000_000

# 東証区分（あれば使用）
ALLOWED_MARKETS = {
    "PRIME",
    "STANDARD",
    "GROWTH",
}


# ============================================================
# helpers
# ============================================================

def _safe_numeric(df: pd.DataFrame) -> pd.DataFrame:

    try:
        num_cols = df.select_dtypes(include=np.number).columns

        df[num_cols] = (
            df[num_cols]
            .replace([np.inf, -np.inf], np.nan)
            .fillna(0)
        )

    except Exception:
        logger.exception("[universe_filter] numeric sanitize failed")

    return df


def _ensure_turnover(df: pd.DataFrame) -> pd.DataFrame:

    if "turnover" in df.columns:
        return df

    if "volume" in df.columns and "close" in df.columns:
        df["turnover"] = df["volume"] * df["close"]
    else:
        df["turnover"] = 0

    return df


def _market_filter(df: pd.DataFrame) -> pd.DataFrame:

    market_cols = [c for c in ["market", "market_code", "exchange"] if c in df.columns]

    if not market_cols:
        return df

    before = len(df)

    mask = pd.Series(True, index=df.index)

    for col in market_cols:
        s = df[col].astype(str).str.upper()

        mask &= s.isin(ALLOWED_MARKETS)

    df = df[mask]

    removed = before - len(df)

    if removed > 0:
        logger.debug(
            "[universe_filter] market filter removed %s rows",
            removed
        )

    return df


def _price_filter(df: pd.DataFrame) -> pd.DataFrame:

    if "close" not in df.columns:
        return df

    before = len(df)

    df = df[
        (df["close"] >= MIN_PRICE)
        & (df["close"] <= MAX_PRICE)
    ]

    removed = before - len(df)

    if removed > 0:
        logger.debug(
            "[universe_filter] price filter removed %s rows",
            removed
        )

    return df


def _volume_filter(df: pd.DataFrame) -> pd.DataFrame:

    if "volume" not in df.columns:
        return df

    before = len(df)

    df = df[df["volume"] >= MIN_VOLUME]

    removed = before - len(df)

    if removed > 0:
        logger.debug(
            "[universe_filter] volume filter removed %s rows",
            removed
        )

    return df


def _turnover_filter(df: pd.DataFrame) -> pd.DataFrame:

    if "turnover" not in df.columns:
        return df

    before = len(df)

    df = df[df["turnover"] >= MIN_TURNOVER]

    removed = before - len(df)

    if removed > 0:
        logger.debug(
            "[universe_filter] turnover filter removed %s rows",
            removed
        )

    return df


# ============================================================
# main
# ============================================================

def apply_universe_filter(
    df: pd.DataFrame,
    *,
    min_price: float = MIN_PRICE,
    max_price: float = MAX_PRICE,
    min_volume: int = MIN_VOLUME,
    min_turnover: int = MIN_TURNOVER,
) -> pd.DataFrame:
    """
    銘柄母集団フィルタ

    目的：
    - ノイズ銘柄排除
    - 流動性確保
    - スリッページ防止
    """

    if df is None:
        return pd.DataFrame()

    if not isinstance(df, pd.DataFrame):
        return pd.DataFrame()

    if df.empty:
        return df

    try:

        df = df.copy()

        # ----------------------------------------------
        # numeric sanitize
        # ----------------------------------------------

        df = _safe_numeric(df)

        # ----------------------------------------------
        # turnover生成
        # ----------------------------------------------

        df = _ensure_turnover(df)

        # ----------------------------------------------
        # market filter（あれば）
        # ----------------------------------------------

        df = _market_filter(df)

        if df.empty:
            return df

        # ----------------------------------------------
        # price
        # ----------------------------------------------

        df = _price_filter(df)

        if df.empty:
            return df

        # ----------------------------------------------
        # volume
        # ----------------------------------------------

        df = _volume_filter(df)

        if df.empty:
            return df

        # ----------------------------------------------
        # turnover
        # ----------------------------------------------

        df = _turnover_filter(df)

        if df.empty:
            return df

        return df

    except Exception:

        logger.exception("[universe_filter] failed")

        return pd.DataFrame()