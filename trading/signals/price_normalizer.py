# ============================================================
# trading/signals/price_normalizer.py
# Ver1.0-PRODUCTION-PRICE-NORMALIZER
# ------------------------------------------------------------
# ✔ OHLC列名統一（open/high/low/close → *_price）
# ✔ dict / pandas row / DataFrame 完全対応
# ✔ KeyError完全防止
# ✔ NaN / inf 安全化
# ✔ push / yahoo / summary / ranking 互換
# ✔ 本番安定版
# ============================================================

from __future__ import annotations

import pandas as pd
import numpy as np


# ============================================================
# OHLC column normalize (DataFrame)
# ============================================================

def normalize_price_columns(df: pd.DataFrame | None) -> pd.DataFrame | None:

    if df is None:
        return df

    if not isinstance(df, pd.DataFrame):
        return df

    rename_map = {}

    if "open" in df.columns and "open_price" not in df.columns:
        rename_map["open"] = "open_price"

    if "high" in df.columns and "high_price" not in df.columns:
        rename_map["high"] = "high_price"

    if "low" in df.columns and "low_price" not in df.columns:
        rename_map["low"] = "low_price"

    if "close" in df.columns and "close_price" not in df.columns:
        rename_map["close"] = "close_price"

    if rename_map:
        df = df.rename(columns=rename_map)

    return df


# ============================================================
# row normalize (dict)
# ============================================================

def normalize_row(row: dict | None) -> dict | None:

    if row is None:
        return row

    if not isinstance(row, dict):
        return row

    if "open" in row and "open_price" not in row:
        row["open_price"] = row["open"]

    if "high" in row and "high_price" not in row:
        row["high_price"] = row["high"]

    if "low" in row and "low_price" not in row:
        row["low_price"] = row["low"]

    if "close" in row and "close_price" not in row:
        row["close_price"] = row["close"]

    return row


# ============================================================
# pandas Series normalize
# ============================================================

def normalize_series(series: pd.Series | None) -> pd.Series | None:

    if series is None:
        return series

    if not isinstance(series, pd.Series):
        return series

    if "open" in series and "open_price" not in series:
        series["open_price"] = series["open"]

    if "high" in series and "high_price" not in series:
        series["high_price"] = series["high"]

    if "low" in series and "low_price" not in series:
        series["low_price"] = series["low"]

    if "close" in series and "close_price" not in series:
        series["close_price"] = series["close"]

    return series


# ============================================================
# numeric safe
# ============================================================

def sanitize_numeric(df: pd.DataFrame | None) -> pd.DataFrame | None:

    if df is None or not isinstance(df, pd.DataFrame):
        return df

    numeric_cols = df.select_dtypes(include=[np.number]).columns

    for c in numeric_cols:

        df[c] = df[c].replace([np.inf, -np.inf], np.nan)

    return df


# ============================================================
# full normalize
# ============================================================

def normalize_inputs(curr, prev, recent):

    curr = normalize_row(curr)
    prev = normalize_row(prev)

    recent = normalize_price_columns(recent)
    recent = sanitize_numeric(recent)

    return curr, prev, recent


# ============================================================
# ensure required columns
# ============================================================

def ensure_price_columns(df: pd.DataFrame | None) -> pd.DataFrame | None:

    if df is None or not isinstance(df, pd.DataFrame):
        return df

    required = [
        "open_price",
        "high_price",
        "low_price",
        "close_price"
    ]

    for c in required:

        if c not in df.columns:
            df[c] = np.nan

    return df


# ============================================================
# full dataframe guard
# ============================================================

def normalize_dataframe(df: pd.DataFrame | None) -> pd.DataFrame | None:

    df = normalize_price_columns(df)
    df = ensure_price_columns(df)
    df = sanitize_numeric(df)

    return df