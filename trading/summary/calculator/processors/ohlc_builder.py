# ============================================================
# File   : trading/summary/calculator/processors/ohlc_builder.py
# Version: Ver3.0-PRODUCTION-OHLC-BUILDER-ULTRA-STABLE
# ------------------------------------------------------------
# ✔ process_data_df wrapper（完全防御）
# ✔ alias修復（優先順位制御）
# ✔ dtype完全安定化
# ✔ datetime完全安全化
# ✔ MultiIndex完全対応
# ✔ duplicate列完全防御
# ✔ NaN / inf完全防御
# ✔ OHLC列名ゆらぎ吸収
# ✔ fallback処理（致命エラー回避）
# ✔ production hardened
# ============================================================

from __future__ import annotations

import logging
import pandas as pd
import numpy as np

from trading.data.process_data_df import process_data_df

logger = logging.getLogger(__name__)


# ============================================================
# alias repair（優先順位付き）
# ============================================================

def _repair_price_alias(df: pd.DataFrame) -> pd.DataFrame:

    if "price" not in df.columns:

        for alt in (
            "price",
            "close",
            "close_price",
            "current_price",
            "last_price",
        ):

            if alt in df.columns:
                df["price"] = df[alt]
                logger.warning("[OHLC BUILDER] price alias used: %s", alt)
                break

    return df


def _repair_volume_alias(df: pd.DataFrame) -> pd.DataFrame:

    if "volume" not in df.columns:

        for alt in (
            "volume",
            "vol",
            "trade_volume",
            "trading_volume",
        ):

            if alt in df.columns:
                df["volume"] = df[alt]
                logger.warning("[OHLC BUILDER] volume alias used: %s", alt)
                break

    if "volume" not in df.columns:
        df["volume"] = 0.0

    return df


def _repair_datetime_alias(df: pd.DataFrame) -> pd.DataFrame:

    if "datetime" not in df.columns:

        for alt in (
            "datetime",
            "timestamp",
            "time",
            "end_time",
            "snapshot_time",
        ):

            if alt in df.columns:
                df["datetime"] = df[alt]
                logger.warning("[OHLC BUILDER] datetime alias used: %s", alt)
                break

    return df


# ============================================================
# input sanitize（強化版）
# ============================================================

def _sanitize_input(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or not isinstance(df, pd.DataFrame):
        return pd.DataFrame()

    if df.empty:
        return df

    df = df.copy()

    # MultiIndex flatten
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = ["_".join(map(str, c)) for c in df.columns]

    # duplicate columns 제거
    if df.columns.duplicated().any():

        dup = df.columns[df.columns.duplicated()].tolist()

        logger.warning("[OHLC BUILDER] duplicate columns removed: %s", dup)

        df = df.loc[:, ~df.columns.duplicated()]

    # alias repair
    df = _repair_price_alias(df)
    df = _repair_volume_alias(df)
    df = _repair_datetime_alias(df)

    # 必須列確認
    required = {"symbol", "datetime", "price", "volume"}

    if not required.issubset(df.columns):
        return pd.DataFrame()

    # dtype安定化
    df["symbol"] = df["symbol"].astype(str)

    df["price"] = pd.to_numeric(df["price"], errors="coerce")
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0)

    # datetime normalize
    df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")

    df = df.dropna(subset=["datetime"])

    # numeric sanitize
    df = df.replace([np.inf, -np.inf], np.nan)

    return df


# ============================================================
# output sanitize（強化版）
# ============================================================

def _sanitize_output(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return pd.DataFrame()

    df = df.copy()

    # index reset
    if isinstance(df.index, (pd.MultiIndex, pd.Index)):
        df = df.reset_index(drop=True)

    # duplicate columns
    if df.columns.duplicated().any():
        df = df.loc[:, ~df.columns.duplicated()]

    # dtype
    if "symbol" in df.columns:
        df["symbol"] = df["symbol"].astype(str)

    if "datetime" in df.columns:
        df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")

    # numeric sanitize
    num_cols = df.select_dtypes(include="number").columns

    if len(num_cols) > 0:
        df[num_cols] = (
            df[num_cols]
            .replace([np.inf, -np.inf], 0)
            .fillna(0)
            .astype("float64")
        )

    return df


# ============================================================
# fallback OHLC（最終防御）
# ============================================================

def _fallback_ohlc(df: pd.DataFrame) -> pd.DataFrame:

    try:

        df["minute"] = df["datetime"].dt.floor("1min")

        grouped = df.groupby(["symbol", "minute"])

        rows = []

        for (symbol, minute), g in grouped:

            g = g.sort_values("datetime")

            rows.append({
                "symbol": symbol,
                "datetime": minute,
                "open_price": g["price"].iloc[0],
                "high_price": g["price"].max(),
                "low_price": g["price"].min(),
                "close_price": g["price"].iloc[-1],
                "volume": g["volume"].sum(),
            })

        return pd.DataFrame(rows)

    except Exception:

        logger.exception("[OHLC BUILDER] fallback failed")

        return pd.DataFrame()


# ============================================================
# main builder
# ============================================================

def build_ohlc(
    df: pd.DataFrame,
    start_time,
    end_time,
    *,
    interval: int = 1,
) -> pd.DataFrame:

    if df is None or not isinstance(df, pd.DataFrame):
        return pd.DataFrame()

    try:

        df = _sanitize_input(df)

        if df.empty:
            logger.warning("[OHLC BUILDER] input empty")
            return pd.DataFrame()

        # ----------------------------------------------------
        # primary engine
        # ----------------------------------------------------

        try:

            ohlc = process_data_df(
                df_to_process=df,
                start_time=start_time,
                end_time=end_time,
                interval=interval,
            )

        except Exception:

            logger.exception("[OHLC BUILDER] process_data_df crashed → fallback")

            ohlc = _fallback_ohlc(df)

        if ohlc is None or ohlc.empty:

            logger.warning("[OHLC BUILDER] empty → fallback")

            ohlc = _fallback_ohlc(df)

        if ohlc is None or ohlc.empty:
            return pd.DataFrame()

        ohlc = _sanitize_output(ohlc)

        # ----------------------------------------------------
        # OHLC列名統一
        # ----------------------------------------------------

        rename_map = {
            "open": "open_price",
            "high": "high_price",
            "low": "low_price",
            "close": "close_price",
        }

        for k, v in rename_map.items():
            if k in ohlc.columns and v not in ohlc.columns:
                ohlc[v] = ohlc[k]

        # ----------------------------------------------------
        # key保証
        # ----------------------------------------------------

        if "symbol" not in ohlc.columns or "datetime" not in ohlc.columns:
            logger.error("[OHLC BUILDER] key missing after build")
            return pd.DataFrame()

        # ----------------------------------------------------
        # sort + duplicate guard
        # ----------------------------------------------------

        ohlc = ohlc.sort_values(["symbol", "datetime"])

        ohlc = ohlc.drop_duplicates(
            ["symbol", "datetime"],
            keep="last"
        ).reset_index(drop=True)

        return ohlc

    except Exception:

        logger.exception("[OHLC BUILDER] fatal error")

        return pd.DataFrame()