# ============================================================
# File   : trading/summary/calculator/calculator_summary.py
# Version: Ver5.0-PRODUCTION-SUMMARY-CALCULATOR-ULTRA-STABLE
# ------------------------------------------------------------
# ✔ PUSH → OHLCV summary
# ✔ symbol grouping
# ✔ VWAP calculation（ゼロ除算防止）
# ✔ volume aggregation
# ✔ datetime generation
# ✔ duplicate guard
# ✔ NaN / inf 完全防御
# ✔ dtype stabilization
# ✔ pandas crash防止
# ✔ production hardened
# ============================================================

from __future__ import annotations

import logging
import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)


# ============================================================
# dataframe sanitizer
# ============================================================

def _sanitize_dataframe(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return pd.DataFrame()

    df = df.copy()

    # MultiIndex flatten
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    # duplicate columns 제거
    if df.columns.duplicated().any():
        df = df.loc[:, ~df.columns.duplicated()]

    df = df.reset_index(drop=True)

    # symbol強制str
    if "symbol" in df.columns:
        df["symbol"] = df["symbol"].astype(str)

    return df


# ============================================================
# datetime safety
# ============================================================

def _safe_datetime(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    if "datetime" not in df.columns:
        return df

    df = df.copy()

    df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")

    before = len(df)

    df = df.dropna(subset=["datetime"])

    dropped = before - len(df)

    if dropped > 0:
        logger.warning(f"[SUMMARY CALC] dropped rows without datetime: {dropped}")

    return df


# ============================================================
# numeric safety
# ============================================================

def _sanitize_numeric(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    df = df.replace([np.inf, -np.inf], np.nan)

    return df


# ============================================================
# VWAP calculation（安全版）
# ============================================================

def _calc_vwap(price: pd.Series, volume: pd.Series):

    try:

        volume_sum = volume.sum()

        if volume_sum == 0:
            return np.nan

        pv = price * volume

        return pv.sum() / volume_sum

    except Exception:
        return np.nan


# ============================================================
# OHLC calculation
# ============================================================

def _ohlc(group: pd.DataFrame):

    try:

        price = group["price"]

        return (
            price.iloc[0],      # open
            price.max(),        # high
            price.min(),        # low
            price.iloc[-1],     # close
        )

    except Exception:

        return np.nan, np.nan, np.nan, np.nan


# ============================================================
# MAIN SUMMARY FUNCTION
# ============================================================

def calculate_summary(
    df_push: pd.DataFrame,
    df_summary: pd.DataFrame | None = None,
    symbols=None,
    start_time=None,
    end_time=None,
    debug: bool = False
) -> pd.DataFrame:

    # --------------------------------------------------------
    # sanitize
    # --------------------------------------------------------

    df_push = _sanitize_dataframe(df_push)
    df_push = _safe_datetime(df_push)

    if df_push.empty:
        logger.warning("[SUMMARY CALC] push dataframe empty")
        return pd.DataFrame(columns=["symbol", "datetime"])

    # --------------------------------------------------------
    # 必須列チェック
    # --------------------------------------------------------

    if "price" not in df_push.columns:
        logger.error("[SUMMARY CALC] missing price column")
        return pd.DataFrame(columns=["symbol", "datetime"])

    if "volume" not in df_push.columns:
        df_push["volume"] = 0.0

    # numeric安全化
    df_push["price"] = pd.to_numeric(df_push["price"], errors="coerce")
    df_push["volume"] = pd.to_numeric(df_push["volume"], errors="coerce").fillna(0)

    df_push = _sanitize_numeric(df_push)

    # --------------------------------------------------------
    # symbol filter
    # --------------------------------------------------------

    if symbols is not None:
        symbols = [str(s) for s in symbols]
        df_push = df_push[df_push["symbol"].isin(symbols)]

    if df_push.empty:
        return pd.DataFrame(columns=["symbol", "datetime"])

    # --------------------------------------------------------
    # time bucket（1分）
    # --------------------------------------------------------

    df_push["minute"] = df_push["datetime"].dt.floor("1min")

    # --------------------------------------------------------
    # groupby処理（高速化）
    # --------------------------------------------------------

    results = []

    grouped = df_push.groupby(["symbol", "minute"], sort=False)

    for (symbol, minute), group in grouped:

        try:

            group = group.sort_values("datetime")

            open_p, high_p, low_p, close_p = _ohlc(group)

            volume = group["volume"].sum()

            vwap = _calc_vwap(group["price"], group["volume"])

            symbolname = None
            if "symbolname" in group.columns:
                symbolname = group["symbolname"].iloc[0]

            start_str = minute.strftime("%H:%M:%S")
            end_dt = minute + pd.Timedelta(minutes=1)
            end_str = end_dt.strftime("%H:%M:%S")

            results.append({
                "symbol": symbol,
                "symbolname": symbolname,
                "date": minute.strftime("%Y-%m-%d"),
                "time_range": f"{start_str}-{end_str}",
                "start_time": start_str,
                "end_time": end_str,
                "datetime": minute,
                "source": "push",

                "open_price": open_p,
                "high_price": high_p,
                "low_price": low_p,
                "close_price": close_p,

                "volume": volume,
                "vwap": vwap,

                "time": start_str,
            })

        except Exception:
            logger.exception(f"[SUMMARY CALC] group failed: {symbol} {minute}")

    df_summary = pd.DataFrame(results)

    # --------------------------------------------------------
    # post sanitize
    # --------------------------------------------------------

    df_summary = _sanitize_dataframe(df_summary)
    df_summary = _safe_datetime(df_summary)
    df_summary = _sanitize_numeric(df_summary)

    if df_summary.empty:
        return df_summary

    # dtype安定化
    if "symbol" in df_summary.columns:
        df_summary["symbol"] = df_summary["symbol"].astype(str)

    if "datetime" in df_summary.columns:
        df_summary["datetime"] = pd.to_datetime(df_summary["datetime"], errors="coerce")

    # --------------------------------------------------------
    # duplicate guard
    # --------------------------------------------------------

    if {"symbol", "datetime"}.issubset(df_summary.columns):
        df_summary = (
            df_summary
            .sort_values(["symbol", "datetime"])
            .drop_duplicates(["symbol", "datetime"], keep="last")
            .reset_index(drop=True)
        )

    # --------------------------------------------------------
    # debug
    # --------------------------------------------------------

    if debug:
        logger.info(
            "[SUMMARY CALC] rows=%s cols=%s",
            len(df_summary),
            len(df_summary.columns),
        )

    return df_summary