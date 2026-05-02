# ============================================================
# File   : trading/summary/summary_builder_master.py
# Version: Ver45-PRODUCTION-MTF-SAFE-FINAL
# ------------------------------------------------------------
# ✔ Ver44 完全保持（削除ゼロ）
# ✔ MTF列KeyError完全防止
# ✔ merge後列保証
# ✔ NaN / inf 完全防御
# ✔ symbol保証
# ✔ datetime保証
# ✔ scheduler crash防止
# ✔ production安定化
# ============================================================

from __future__ import annotations
import datetime as dt
import pandas as pd
import numpy as np
import logging

from global_state import global_data
from trading.summary.indicators.indicator_calculator import add_all_indicators
from trading.aggregation.hybrid_1m_engine import build_confirmed_push_1min

logger = logging.getLogger(__name__)

MARKET_OPEN  = dt.time(9, 0)
MARKET_CLOSE = dt.time(15, 29)


# ============================================================
# Utility
# ============================================================

def _safe_numeric(s, default=0.0):

    if s is None:
        return pd.Series([], dtype="float64")

    return (
        pd.to_numeric(s, errors="coerce")
        .replace([np.inf, -np.inf], default)
        .fillna(default)
        .astype("float64")
    )


def _safe_div(a, b):

    if b in (0, None) or pd.isna(b):
        return 0.0

    return a / b


def _ensure_columns(df: pd.DataFrame, cols):

    for c in cols:
        if c not in df.columns:
            df[c] = 0.0

    return df


# ============================================================
# OHLC canonical 保証
# ============================================================

def _normalize_ohlc(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    df = df.copy()

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    df.columns = [str(c).strip() for c in df.columns]
    lower_map = {c.lower(): c for c in df.columns}

    if df.columns.duplicated().any():
        df = df.loc[:, ~df.columns.duplicated()]

    canonical_cols = {"open_price", "high_price", "low_price", "close_price"}

    if canonical_cols.issubset(set(df.columns)):

        for col in canonical_cols:
            df[col] = (
                pd.to_numeric(df[col], errors="coerce")
                .replace([np.inf, -np.inf], np.nan)
            )

        if "volume" in df.columns:
            df["volume"] = (
                pd.to_numeric(df["volume"], errors="coerce")
                .replace([np.inf, -np.inf], 0.0)
                .fillna(0.0)
            )

        df["close"] = df["close_price"]

    else:

        col_map = {
            "open": "open_price",
            "high": "high_price",
            "low": "low_price",
            "close": "close_price",
            "volume": "volume",
        }

        for src_lower, dst in col_map.items():

            if src_lower in lower_map:

                real_col = lower_map[src_lower]

                val = df[real_col]

                if isinstance(val, pd.DataFrame):
                    val = val.iloc[:, 0]

                df[dst] = val

        for col in ["open_price", "high_price", "low_price", "close_price", "volume"]:

            if col in df.columns:
                df[col] = (
                    pd.to_numeric(df[col], errors="coerce")
                    .replace([np.inf, -np.inf], np.nan)
                )

        if "close_price" in df.columns:
            df["close"] = df["close_price"]

    if "close" not in df.columns and "close_price" in df.columns:
        df["close"] = df["close_price"]

    if "close" in df.columns:
        df["close"] = (
            pd.to_numeric(df["close"], errors="coerce")
            .replace([np.inf, -np.inf], np.nan)
        )

    if "Datetime" in df.columns:
        df["datetime"] = pd.to_datetime(df["Datetime"], errors="coerce")

    elif "datetime" in df.columns:
        df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")

    elif isinstance(df.index, pd.DatetimeIndex):
        df["datetime"] = df.index

    else:
        df["datetime"] = pd.NaT

    df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")

    if "close" in df.columns and df["close"].isna().all():
        logger.warning("close all NaN detected")

    return df
# ============================================================
# MASTER 1MIN BUILDER
# ============================================================

def build_master_1min(yahoo_1min: pd.DataFrame, push_1min: pd.DataFrame):
    """
    Yahoo + Push → master 1min
    priority
    push > yahoo
    """

    try:

        if yahoo_1min is None:
            yahoo_1min = pd.DataFrame()

        if push_1min is None:
            push_1min = pd.DataFrame()

        df = pd.concat(
            [yahoo_1min, push_1min],
            ignore_index=True
        )

        if df.empty:
            return df

        df["datetime"] = pd.to_datetime(
            df["datetime"],
            errors="coerce"
        )

        df = df.dropna(subset=["datetime"])

        if "source" not in df.columns:
            df["source"] = "unknown"

        df["priority"] = df["source"].map({
            "push": 2,
            "yahoo": 1
        }).fillna(0)

        df = df.sort_values(
            ["symbol", "datetime", "priority"]
        )

        df = df.drop_duplicates(
            subset=["symbol", "datetime"],
            keep="last"
        )

        df = df.drop(columns=["priority"], errors="ignore")

        df = df.sort_values(
            ["symbol", "datetime"]
        )

        df = df.reset_index(drop=True)

        logger.info(
            "[MASTER 1MIN] rows=%s",
            len(df)
        )

        return df

    except Exception:

        logger.exception(
            "[MASTER 1MIN] build failed"
        )

        return pd.DataFrame()
# ============================================================
# メイン（1min + MTF統合）
# ============================================================

def build_all_summaries_every_minute(
    *,
    yahoo_1min,
    push_raw,
    summary_3min_cache,
    summary_5min_cache,
    now: dt.datetime,
    dump_score_log: bool = False,
):

    try:

        push_1min = build_confirmed_push_1min(push_raw, now=now)

        master_1min = build_master_1min(yahoo_1min, push_1min)

        if master_1min is None or master_1min.empty:

            logger.warning("[SUMMARY] master_1min empty")

            return {
                "summary_1min": pd.DataFrame(),
                "summary_3min": pd.DataFrame(),
                "summary_5min": pd.DataFrame(),
                "mtf_summary": pd.DataFrame(),
                "score_log": None,
            }

        master_1min = _normalize_ohlc(master_1min)

        master_1min["close"] = _safe_numeric(
            master_1min.get("close", master_1min.get("close_price", 0))
        )

        master_1min["volume"] = _safe_numeric(
            master_1min.get("volume", 0)
        )

        master_1min["turnover"] = master_1min["close"] * master_1min["volume"]

        summary_1min = add_all_indicators(master_1min, interval="1min")

        summary_1min = _ensure_columns(
            summary_1min,
            [
                "symbol",
                "datetime",
            ],
        )

        # =====================================================
        # MTF 3min
        # =====================================================

        if summary_3min_cache is not None and not summary_3min_cache.empty:

            df3 = summary_3min_cache.copy()

            if "ma75_slope" in df3.columns and "atr" in df3.columns:

                df3["slope_atr_scaled_3m"] = df3.apply(
                    lambda r: _safe_div(r["ma75_slope"], r["atr"]),
                    axis=1,
                )

            else:

                df3["slope_atr_scaled_3m"] = 0.0

            df3 = _ensure_columns(df3, ["symbol", "slope_atr_scaled_3m"])

            summary_1min = summary_1min.merge(
                df3[["symbol", "slope_atr_scaled_3m"]],
                on="symbol",
                how="left",
            )

        # =====================================================
        # MTF 5min
        # =====================================================

        if summary_5min_cache is not None and not summary_5min_cache.empty:

            df5 = summary_5min_cache.copy()

            if "ma75_slope" in df5.columns and "atr" in df5.columns:

                df5["slope_atr_scaled_5m"] = df5.apply(
                    lambda r: _safe_div(r["ma75_slope"], r["atr"]),
                    axis=1,
                )

            else:

                df5["slope_atr_scaled_5m"] = 0.0

            df5 = _ensure_columns(df5, ["symbol", "slope_atr_scaled_5m"])

            summary_1min = summary_1min.merge(
                df5[["symbol", "slope_atr_scaled_5m"]],
                on="symbol",
                how="left",
            )

        # =====================================================
        # MTF列最終保証
        # =====================================================

        summary_1min = _ensure_columns(
            summary_1min,
            [
                "slope_atr_scaled_3m",
                "slope_atr_scaled_5m",
            ],
        )

        summary_1min["slope_atr_scaled_3m"] = (
            summary_1min["slope_atr_scaled_3m"].fillna(0.0)
        )

        summary_1min["slope_atr_scaled_5m"] = (
            summary_1min["slope_atr_scaled_5m"].fillna(0.0)
        )

        if summary_1min.columns.duplicated().any():

            summary_1min = summary_1min.loc[
                :, ~summary_1min.columns.duplicated()
            ]

        summary_1min = (
            summary_1min
            .sort_values(["symbol", "datetime"])
            .drop_duplicates(
                subset=["symbol", "datetime"],
                keep="last",
            )
            .reset_index(drop=True)
        )
        if "symbol" in summary_1min.columns:

            symbol_map = getattr(global_data, "symbol_name_map", {})

            if symbol_map:
                summary_1min["symbolname"] = (
                    summary_1min["symbol"]
                    .astype(str)
                    .map(symbol_map)
                    .fillna(summary_1min["symbol"])
                )
        logger.info(
            "[SUMMARY 1M FINAL] rows=%d symbols=%d",
            len(summary_1min),
            summary_1min["symbol"].nunique(),
        )

        return {
            "summary_1min": summary_1min,
            "summary_3min": pd.DataFrame(),
            "summary_5min": pd.DataFrame(),
            "mtf_summary": pd.DataFrame(),
            "score_log": None,
        }

    except Exception:

        logger.exception("[SUMMARY] fatal")

        return {
            "summary_1min": pd.DataFrame(),
            "summary_3min": pd.DataFrame(),
            "summary_5min": pd.DataFrame(),
            "mtf_summary": pd.DataFrame(),
            "score_log": None,
        }