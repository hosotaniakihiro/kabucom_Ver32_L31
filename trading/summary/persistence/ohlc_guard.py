# ============================================================
# File   : trading/summary/persistence/ohlc_guard.py
# Version: Ver1.0-PRODUCTION-OHLC-GUARD
# ------------------------------------------------------------
# 機能:
# - OHLC妥当性判定
# - invalid OHLC row 除去
# - dead price row 除去
# - 各価格候補列からのprice抽出
# ------------------------------------------------------------
# 主な責務:
# - DB保存前に異常価格データを除外
# - 1分足は close 生存を最低条件として許容補完
# ============================================================

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from trading.summary.persistence.dataframe_utils import ensure_dataframe, safe_get_series

logger = logging.getLogger(__name__)


def pick_price_series(df: pd.DataFrame, candidates: list[str]) -> pd.Series:
    idx = df.index
    base = pd.Series(np.nan, index=idx, dtype="float64")
    for c in candidates:
        if c in df.columns:
            try:
                s = pd.to_numeric(safe_get_series(df, c), errors="coerce").replace([np.inf, -np.inf], np.nan)
                base = base.combine_first(s)
            except Exception:
                logger.debug("[SUMMARY] pick price failed col=%s", c, exc_info=True)
    return base


def ohlc_valid_mask(df: pd.DataFrame, interval: int) -> pd.Series:
    if df is None or df.empty:
        return pd.Series(dtype=bool)

    out = ensure_dataframe(df)
    idx = out.index

    open_s = pick_price_series(out, ["open", "open_price", "OpenPrice", "openPrice"])
    high_s = pick_price_series(out, ["high", "high_price", "HighPrice", "highPrice"])
    low_s = pick_price_series(out, ["low", "low_price", "LowPrice", "lowPrice"])
    close_s = pick_price_series(out, [
        "close", "close_price", "ClosePrice", "closePrice",
        "price", "current_price", "CurrentPrice", "last_price", "LastPrice",
    ])
    volume_s = pick_price_series(out, ["volume", "Volume", "trading_volume", "TradingVolume"])

    open_s = open_s.mask(open_s <= 0, np.nan)
    high_s = high_s.mask(high_s <= 0, np.nan)
    low_s = low_s.mask(low_s <= 0, np.nan)
    close_s = close_s.mask(close_s <= 0, np.nan)
    volume_s = volume_s.mask(volume_s < 0, np.nan)

    if int(interval) == 1:
        open_s = open_s.combine_first(close_s)
        high_s = high_s.combine_first(close_s)
        low_s = low_s.combine_first(close_s)

        valid = (
            close_s.notna()
            & open_s.notna()
            & high_s.notna()
            & low_s.notna()
            & (high_s >= low_s)
            & (high_s >= open_s)
            & (high_s >= close_s)
            & (low_s <= open_s)
            & (low_s <= close_s)
        )
        return valid.reindex(idx, fill_value=False)

    valid = (
        open_s.notna()
        & high_s.notna()
        & low_s.notna()
        & close_s.notna()
        & (high_s >= low_s)
        & (high_s >= open_s)
        & (high_s >= close_s)
        & (low_s <= open_s)
        & (low_s <= close_s)
    )
    return valid.reindex(idx, fill_value=False)


def drop_invalid_ohlc_rows(df: pd.DataFrame, interval: int, stage: str) -> pd.DataFrame:
    if df is None or df.empty:
        return df

    out = ensure_dataframe(df)
    if out.empty:
        return out

    valid = ohlc_valid_mask(out, interval)
    if valid.empty:
        return out

    before = len(out)
    bad = out.loc[~valid].copy()

    if not bad.empty:
        sample_cols = [
            c for c in [
                "symbol", "symbolname", "datetime", "date", "time_range", "source",
                "open", "high", "low", "close",
                "open_price", "high_price", "low_price", "close_price",
                "price", "current_price", "CurrentPrice", "last_price", "LastPrice",
                "volume", "trading_volume", "TradingVolume",
            ] if c in bad.columns
        ]
        logger.warning(
            "[SUMMARY] invalid OHLC rows removed stage=%s interval=%s removed=%s sample=\n%s",
            stage,
            interval,
            len(bad),
            bad[sample_cols].head(20).to_string(index=False),
        )

    out = out.loc[valid].copy()
    removed = before - len(out)

    if removed > 0:
        logger.warning(
            "[SUMMARY] invalid OHLC removed stage=%s interval=%s before=%d after=%d",
            stage,
            interval,
            before,
            len(out),
        )

    return out


def drop_dead_price_rows(df: pd.DataFrame, interval: int, stage: str) -> pd.DataFrame:
    if df is None or df.empty:
        return df

    out = ensure_dataframe(df)
    if out.empty:
        return out

    close_s = pick_price_series(out, [
        "close", "close_price", "price",
        "current_price", "CurrentPrice", "last_price", "LastPrice",
    ])
    close_s = close_s.mask(close_s <= 0, np.nan)

    before = len(out)
    out = out.loc[close_s.notna()].copy()

    removed = before - len(out)
    if removed > 0:
        logger.warning(
            "[SUMMARY] dead price rows removed stage=%s interval=%s before=%d after=%d",
            stage,
            interval,
            before,
            len(out),
        )

    return out