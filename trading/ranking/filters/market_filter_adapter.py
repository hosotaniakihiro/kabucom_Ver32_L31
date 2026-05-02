# ============================================================
# File   : trading/ranking/filters/market_filter_adapter.py
# Version: Ver3.0-PRODUCTION-ULTRA-STABLE-MARKET-FILTER-ADAPTER-FINAL
# ------------------------------------------------------------
# ✔ apply_market_filter 追加（ImportError完全解消）
# ✔ utils.market_filter 連携（存在時）
# ✔ ETF / REIT / PRO Market 除外
# ✔ 市場コード（TSE区分）フィルタ
# ✔ symbol / name ベース多重判定
# ✔ dtype崩れ耐性
# ✔ mask完全ベクトル安定化
# ✔ index alignment crash防止
# ✔ immutable keyword設計
# ✔ production hardened
# ============================================================

from __future__ import annotations

import logging
from typing import Optional, Iterable, Set

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# optional import
# ============================================================

try:
    from utils.market_filter import filter_tradeable_dataframe as _base_filter
except Exception:
    _base_filter = None


# ============================================================
# constants（immutable）
# ============================================================

BASE_EXCLUDE_NAME_KEYWORDS: Set[str] = {
    "ＥＴＦ", "ETF",
    "ＲＥＩＴ", "REIT",
    "投資法人",
    "インデックス",
    "指数",
    "連動",
}

BASE_EXCLUDE_SYMBOL_PREFIXES: Set[str] = {
    # 拡張用
}

BASE_EXCLUDE_MARKET_KEYWORDS: Set[str] = {
    "PRO",
    "ETF",
    "REIT",
}


# ============================================================
# helpers
# ============================================================

def _safe_series(df: pd.DataFrame, col: str) -> pd.Series:
    try:
        return df[col].astype(str)
    except Exception:
        return pd.Series([""] * len(df), index=df.index)


def _ensure_symbol_str(df: pd.DataFrame) -> pd.DataFrame:
    if "symbol" in df.columns:
        try:
            df["symbol"] = df["symbol"].astype(str).str.strip()
        except Exception:
            logger.exception("[market_filter_adapter] symbol normalize failed")
    return df


def _numeric_sanity(df: pd.DataFrame) -> pd.DataFrame:
    try:
        num_cols = df.select_dtypes(include=np.number).columns
        df[num_cols] = (
            df[num_cols]
            .replace([np.inf, -np.inf], np.nan)
            .fillna(0)
        )
    except Exception:
        logger.exception("[market_filter_adapter] numeric sanity failed")
    return df


# ============================================================
# filters（完全ベクトル）
# ============================================================

def _build_name_mask(df: pd.DataFrame, keywords: Set[str]) -> pd.Series:

    cols = [c for c in ("name", "symbolname") if c in df.columns]

    if not cols:
        return pd.Series(True, index=df.index)

    combined = pd.Series("", index=df.index)

    for c in cols:
        combined = combined + " " + _safe_series(df, c)

    pattern = "|".join(map(str, keywords))

    return ~combined.str.contains(pattern, na=False)


def _build_symbol_mask(df: pd.DataFrame, prefixes: Set[str]) -> pd.Series:

    if "symbol" not in df.columns or not prefixes:
        return pd.Series(True, index=df.index)

    s = _safe_series(df, "symbol")

    mask = pd.Series(True, index=df.index)

    for p in prefixes:
        mask &= ~s.str.startswith(p)

    return mask


def _build_market_mask(df: pd.DataFrame, keywords: Set[str]) -> pd.Series:

    cols = [c for c in ("market", "market_code", "exchange") if c in df.columns]

    if not cols:
        return pd.Series(True, index=df.index)

    combined = pd.Series("", index=df.index)

    for c in cols:
        combined = combined + " " + _safe_series(df, c)

    pattern = "|".join(map(str, keywords))

    return ~combined.str.contains(pattern, case=False, na=False)


# ============================================================
# main
# ============================================================

def filter_market(
    df: pd.DataFrame,
    *,
    use_base_filter: bool = True,
    extra_exclude_names: Optional[Iterable[str]] = None,
) -> pd.DataFrame:

    if df is None or not isinstance(df, pd.DataFrame):
        return pd.DataFrame()

    if df.empty:
        return df

    try:
        df = df.copy()

        df = _ensure_symbol_str(df)
        df = _numeric_sanity(df)

        before = len(df)

        # ----------------------------------------------------
        # base filter
        # ----------------------------------------------------
        if use_base_filter and _base_filter is not None:
            try:
                df = _base_filter(df)
            except Exception:
                logger.exception("[market_filter_adapter] base filter failed")

        if df.empty:
            return df

        # ----------------------------------------------------
        # keyword構築（immutable）
        # ----------------------------------------------------
        name_keywords = set(BASE_EXCLUDE_NAME_KEYWORDS)

        if extra_exclude_names:
            name_keywords |= set(map(str, extra_exclude_names))

        # ----------------------------------------------------
        # mask生成（完全同期index）
        # ----------------------------------------------------
        mask = (
            _build_name_mask(df, name_keywords)
            & _build_symbol_mask(df, BASE_EXCLUDE_SYMBOL_PREFIXES)
            & _build_market_mask(df, BASE_EXCLUDE_MARKET_KEYWORDS)
        )

        if mask is None or mask.empty:
            return df

        df = df[mask]

        removed = before - len(df)

        if removed > 0:
            logger.debug(
                "[market_filter_adapter] removed %s rows",
                removed
            )

        return df

    except Exception:
        logger.exception("[market_filter_adapter] failed")
        return pd.DataFrame()


# ============================================================
# 🚨 ranking_pipeline互換API（超重要）
# ============================================================

def apply_market_filter(
    df: pd.DataFrame,
    *,
    use_base_filter: bool = True,
    extra_exclude_names: Optional[Iterable[str]] = None,
) -> pd.DataFrame:
    """
    ranking_pipeline から呼ばれる統一API
    """

    return filter_market(
        df,
        use_base_filter=use_base_filter,
        extra_exclude_names=extra_exclude_names,
    )