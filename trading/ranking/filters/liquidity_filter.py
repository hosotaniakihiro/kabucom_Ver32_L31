# ============================================================
# File   : trading/ranking/filters/liquidity_filter.py
# Version: Ver3.0-PRODUCTION-ULTRA-STABLE-LIQUIDITY-FILTER-MINPRICE200
# ------------------------------------------------------------
# ✔ Ver2.0 機能完全保持（削除ゼロ）
# ✔ numeric sanitize 強化
# ✔ dtype安全化
# ✔ turnover強制生成
# ✔ VWAP sanity強化
# ✔ negative値防止
# ✔ 極端値クリップ
# ✔ early return最適化
# ✔ pandas alignment crash防止
# ✔ production hardened
# ✔ 200円未満の銘柄を除外
# ✔ close / close_price / current_price / price を価格列として認識
# ✔ volume / trading_volume を出来高列として認識
# ✔ turnover / trading_value を売買代金列として認識
# ✔ サマリー表示・エントリー前・保存前の互換aliasを提供
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Iterable, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# default thresholds
# ============================================================

MIN_VOLUME = int(float(os.environ.get("LIQUIDITY_MIN_VOLUME", "10000")))
MIN_TURNOVER = int(float(os.environ.get("LIQUIDITY_MIN_TURNOVER", "5000000")))

# 重要:
#   100円ではなく200円。
#   8918 ランド、9973 KOZO のような低位株をここで落とす。
MIN_PRICE = float(os.environ.get("LIQUIDITY_MIN_PRICE", "200"))

MAX_PRICE = float(os.environ.get("LIQUIDITY_MAX_PRICE", "10000"))

ENABLE_LIQUIDITY_FILTER = str(
    os.environ.get("ENABLE_LIQUIDITY_FILTER", "1")
).strip().lower() not in {"0", "false", "no", "off", "ng"}

REQUIRE_PRICE_COLUMN = str(
    os.environ.get("LIQUIDITY_REQUIRE_PRICE_COLUMN", "1")
).strip().lower() not in {"0", "false", "no", "off", "ng"}


PRICE_COLUMNS = (
    "close",
    "close_price",
    "current_price",
    "price",
    "last_price",
)

VOLUME_COLUMNS = (
    "volume",
    "trading_volume",
)

TURNOVER_COLUMNS = (
    "turnover",
    "trading_value",
)


# ============================================================
# helpers
# ============================================================

def _first_existing_col(df: pd.DataFrame, candidates: Iterable[str]) -> Optional[str]:
    for c in candidates:
        if c in df.columns:
            return c
    return None


def _to_numeric_col(df: pd.DataFrame, col: str, default: float = 0.0) -> pd.Series:
    if col not in df.columns:
        return pd.Series([default] * len(df), index=df.index, dtype="float64")

    return pd.to_numeric(df[col], errors="coerce").replace(
        [np.inf, -np.inf], np.nan
    ).fillna(default)


def _symbol_head(df: pd.DataFrame, limit: int = 20) -> list[str]:
    if df is None or df.empty or "symbol" not in df.columns:
        return []

    try:
        return (
            df["symbol"]
            .dropna()
            .astype(str)
            .drop_duplicates()
            .head(limit)
            .tolist()
        )
    except Exception:
        return []


def _ensure_standard_columns(df: pd.DataFrame) -> pd.DataFrame:
    """
    close / volume / turnover の標準列を可能な限り作る。

    既存コード互換のため、最終的に以下をできるだけ揃える:
      - close
      - volume
      - turnover
    """
    if df is None or df.empty:
        return df

    out = df

    price_col = _first_existing_col(out, PRICE_COLUMNS)
    volume_col = _first_existing_col(out, VOLUME_COLUMNS)
    turnover_col = _first_existing_col(out, TURNOVER_COLUMNS)

    if "close" not in out.columns and price_col:
        out["close"] = _to_numeric_col(out, price_col, default=0.0)

    if "volume" not in out.columns and volume_col:
        out["volume"] = _to_numeric_col(out, volume_col, default=0.0)

    if "turnover" not in out.columns and turnover_col:
        out["turnover"] = _to_numeric_col(out, turnover_col, default=0.0)

    return out


def _ensure_turnover(df: pd.DataFrame) -> pd.DataFrame:
    """
    Ensure turnover column exists.
    turnover = volume * close
    """
    if df is None or df.empty:
        return df

    if "turnover" in df.columns:
        return df

    try:
        if "volume" in df.columns and "close" in df.columns:
            volume = _to_numeric_col(df, "volume", default=0.0)
            close = _to_numeric_col(df, "close", default=0.0)
            df["turnover"] = volume * close
        else:
            df["turnover"] = 0.0
    except Exception:
        logger.exception("[liquidity_filter] turnover creation failed")
        df["turnover"] = 0.0

    return df


def _sanitize_numeric(df: pd.DataFrame) -> pd.DataFrame:
    """
    Replace inf / NaN and stabilize numeric dtype.
    """
    if df is None or df.empty:
        return df

    try:
        # object → numeric（可能なものだけ）
        for col in df.columns:
            if df[col].dtype == "object":
                try:
                    df[col] = pd.to_numeric(df[col], errors="ignore")
                except Exception:
                    pass

        num_cols = df.select_dtypes(include=np.number).columns

        if len(num_cols) > 0:
            df[num_cols] = (
                df[num_cols]
                .replace([np.inf, -np.inf], np.nan)
                .fillna(0)
            )

            # 異常値クリップ（広め）
            df[num_cols] = df[num_cols].clip(-1e12, 1e12)

        # negative防止（主要列）
        for c in ("volume", "trading_volume"):
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0).clip(lower=0)

        for c in ("turnover", "trading_value"):
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0).clip(lower=0)

        for c in PRICE_COLUMNS:
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0).clip(lower=0)

        if "vwap" in df.columns:
            df["vwap"] = pd.to_numeric(df["vwap"], errors="coerce").fillna(0).clip(lower=0)

    except Exception:
        logger.exception("[liquidity_filter] numeric sanitize failed")

    return df


# ============================================================
# filters
# ============================================================

def _volume_filter(
    df: pd.DataFrame,
    min_volume: int,
) -> pd.DataFrame:
    volume_col = _first_existing_col(df, VOLUME_COLUMNS)

    if not volume_col:
        return df

    before = len(df)

    volume = _to_numeric_col(df, volume_col, default=0.0)
    out = df[volume >= float(min_volume)].copy()

    removed = before - len(out)

    if removed > 0:
        logger.info(
            "[liquidity_filter] volume filter removed=%s min_volume=%s col=%s removed_head=%s",
            removed,
            min_volume,
            volume_col,
            _symbol_head(df.loc[~df.index.isin(out.index)]),
        )

    return out


def _turnover_filter(
    df: pd.DataFrame,
    min_turnover: int,
) -> pd.DataFrame:
    turnover_col = _first_existing_col(df, TURNOVER_COLUMNS)

    if not turnover_col:
        return df

    before = len(df)

    turnover = _to_numeric_col(df, turnover_col, default=0.0)
    out = df[turnover >= float(min_turnover)].copy()

    removed = before - len(out)

    if removed > 0:
        logger.info(
            "[liquidity_filter] turnover filter removed=%s min_turnover=%s col=%s removed_head=%s",
            removed,
            min_turnover,
            turnover_col,
            _symbol_head(df.loc[~df.index.isin(out.index)]),
        )

    return out


def _price_filter(
    df: pd.DataFrame,
    min_price: float,
    max_price: float,
    *,
    require_price: bool = REQUIRE_PRICE_COLUMN,
) -> pd.DataFrame:
    price_col = _first_existing_col(df, PRICE_COLUMNS)

    if not price_col:
        if require_price:
            logger.warning(
                "[liquidity_filter] price column missing -> drop all rows rows=%d cols=%s",
                len(df),
                list(df.columns),
            )
            return df.iloc[0:0].copy()

        logger.warning(
            "[liquidity_filter] price column missing but require_price=False -> skip price filter rows=%d",
            len(df),
        )
        return df

    before = len(df)

    price = _to_numeric_col(df, price_col, default=0.0)

    out = df[
        (price >= float(min_price))
        & (price <= float(max_price))
    ].copy()

    removed = before - len(out)

    if removed > 0:
        logger.info(
            "[liquidity_filter] price filter removed=%s min_price=%.1f max_price=%.1f col=%s removed_head=%s",
            removed,
            min_price,
            max_price,
            price_col,
            _symbol_head(df.loc[~df.index.isin(out.index)]),
        )

    return out


def _vwap_guard(df: pd.DataFrame) -> pd.DataFrame:
    """
    Remove rows with broken VWAP values.
    強化版：0 / NaN / 異常値除去
    """
    if "vwap" not in df.columns:
        return df

    before = len(df)

    vwap = pd.to_numeric(df["vwap"], errors="coerce")

    mask = (
        vwap.notna()
        & (vwap > 0)
        & np.isfinite(vwap)
    )

    out = df[mask].copy()

    removed = before - len(out)

    if removed > 0:
        logger.info(
            "[liquidity_filter] vwap guard removed=%s removed_head=%s",
            removed,
            _symbol_head(df.loc[~df.index.isin(out.index)]),
        )

    return out


# ============================================================
# main filter
# ============================================================

def apply_liquidity_filter(
    df: pd.DataFrame,
    *,
    min_volume: int = MIN_VOLUME,
    min_turnover: int = MIN_TURNOVER,
    min_price: float = MIN_PRICE,
    max_price: float = MAX_PRICE,
    require_price: bool = REQUIRE_PRICE_COLUMN,
    context: str = "",
) -> pd.DataFrame:
    """
    Apply liquidity filters.

    Filters applied:
      1. numeric sanitize
      2. standard columns
      3. turnover creation
      4. price range
      5. volume
      6. turnover
      7. VWAP sanity

    重要:
      200円未満を必ず落としたいので、価格filterは先に実行する。
    """
    if df is None:
        return pd.DataFrame()

    if not isinstance(df, pd.DataFrame):
        return pd.DataFrame()

    if df.empty:
        return df

    if not ENABLE_LIQUIDITY_FILTER:
        return df

    try:
        before_rows = len(df)
        before_symbols = df["symbol"].nunique() if "symbol" in df.columns else 0

        out = df.copy()

        out = _sanitize_numeric(out)
        out = _ensure_standard_columns(out)
        out = _ensure_turnover(out)
        out = _sanitize_numeric(out)

        # ----------------------------------------------------
        # price
        # ----------------------------------------------------
        out = _price_filter(
            out,
            min_price=float(min_price),
            max_price=float(max_price),
            require_price=require_price,
        )

        if out.empty:
            logger.info(
                "[liquidity_filter] empty after price filter context=%s before_rows=%d min_price=%.1f",
                context,
                before_rows,
                min_price,
            )
            return out

        # ----------------------------------------------------
        # volume
        # ----------------------------------------------------
        if min_volume > 0:
            out = _volume_filter(out, int(min_volume))

            if out.empty:
                logger.info(
                    "[liquidity_filter] empty after volume filter context=%s before_rows=%d",
                    context,
                    before_rows,
                )
                return out

        # ----------------------------------------------------
        # turnover
        # ----------------------------------------------------
        if min_turnover > 0:
            out = _turnover_filter(out, int(min_turnover))

            if out.empty:
                logger.info(
                    "[liquidity_filter] empty after turnover filter context=%s before_rows=%d",
                    context,
                    before_rows,
                )
                return out

        # ----------------------------------------------------
        # VWAP
        # ----------------------------------------------------
        out = _vwap_guard(out)

        if out.empty:
            logger.info(
                "[liquidity_filter] empty after vwap guard context=%s before_rows=%d",
                context,
                before_rows,
            )
            return out

        after_rows = len(out)
        after_symbols = out["symbol"].nunique() if "symbol" in out.columns else 0

        logger.info(
            "[liquidity_filter] done context=%s before_rows=%d after_rows=%d removed_rows=%d "
            "before_symbols=%d after_symbols=%d min_price=%.1f min_volume=%s min_turnover=%s",
            context,
            before_rows,
            after_rows,
            before_rows - after_rows,
            before_symbols,
            after_symbols,
            min_price,
            min_volume,
            min_turnover,
        )

        return out

    except Exception:
        logger.exception("[liquidity_filter] failed context=%s", context)
        return pd.DataFrame()


# ============================================================
# summary aliases / compatibility
# ============================================================

def apply_summary_liquidity_filter(
    df: pd.DataFrame,
    *,
    context: str = "",
    min_price: Optional[float] = None,
    min_volume: Optional[float] = None,
    min_trading_value: Optional[float] = None,
    require_price: bool = True,
) -> pd.DataFrame:
    return apply_liquidity_filter(
        df,
        min_price=MIN_PRICE if min_price is None else float(min_price),
        min_volume=MIN_VOLUME if min_volume is None else int(min_volume),
        min_turnover=MIN_TURNOVER if min_trading_value is None else int(min_trading_value),
        require_price=require_price,
        context=context,
    )


def filter_summary_before_display(
    df: pd.DataFrame,
    *,
    context: str = "summary_display",
) -> pd.DataFrame:
    return apply_summary_liquidity_filter(
        df,
        context=context,
        min_price=MIN_PRICE,
        require_price=True,
    )


def filter_summary_before_entry(
    df: pd.DataFrame,
    *,
    context: str = "summary_entry",
) -> pd.DataFrame:
    return apply_summary_liquidity_filter(
        df,
        context=context,
        min_price=MIN_PRICE,
        require_price=True,
    )


def filter_summary_before_persist(
    df: pd.DataFrame,
    *,
    context: str = "summary_persist",
) -> pd.DataFrame:
    return apply_summary_liquidity_filter(
        df,
        context=context,
        min_price=MIN_PRICE,
        require_price=True,
    )


def filter_low_price_symbols(
    df: pd.DataFrame,
    *,
    context: str = "",
) -> pd.DataFrame:
    return apply_summary_liquidity_filter(
        df,
        context=context or "low_price",
        min_price=MIN_PRICE,
        require_price=True,
    )


__all__ = [
    "MIN_VOLUME",
    "MIN_TURNOVER",
    "MIN_PRICE",
    "MAX_PRICE",
    "ENABLE_LIQUIDITY_FILTER",
    "apply_liquidity_filter",
    "apply_summary_liquidity_filter",
    "filter_summary_before_display",
    "filter_summary_before_entry",
    "filter_summary_before_persist",
    "filter_low_price_symbols",
]