# ============================================================
# File   : trading/features/feature_engine.py
# Version: Ver5.2-PRODUCTION-FEATURE-ENGINE-INSTITUTIONAL-ULTRA-STABLE
# ------------------------------------------------------------
# ✔ Ver5.0 完全保持（削除ゼロ）
# ✔ duplicate index防御
# ✔ OHLC保証
# ✔ Series/DataFrame列混在防御
# ✔ pandas alignment crash完全防止
# ✔ ranking features保持
# ✔ VWAP deviation保持
# ✔ 本番超安定版
# ============================================================

from __future__ import annotations

import logging
import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)


# ============================================================
# dataframe sanitize
# ============================================================

def _sanitize_dataframe(df: pd.DataFrame) -> pd.DataFrame:

    if not isinstance(df, pd.DataFrame):
        return pd.DataFrame()

    if df.empty:
        return df

    try:

        if isinstance(df.columns, pd.MultiIndex):

            df.columns = [
                "_".join([str(x) for x in col if x not in (None, "")])
                for col in df.columns
            ]

        df.columns = [str(c) for c in df.columns]

        if df.columns.duplicated().any():

            dup = list(df.columns[df.columns.duplicated()])

            logger.warning(
                "[FEATURE ENGINE] duplicate columns removed: %s",
                dup,
            )

            df = df.loc[:, ~df.columns.duplicated(keep="last")]

    except Exception:
        logger.exception("[FEATURE ENGINE] dataframe sanitize failed")

    return df


# ============================================================
# duplicate column guard
# ============================================================

def _remove_duplicate_columns(df):

    if df.columns.duplicated().any():

        dup = list(df.columns[df.columns.duplicated()])

        logger.warning(
            "[FEATURE ENGINE] duplicate columns removed: %s",
            dup
        )

        df = df.loc[:, ~df.columns.duplicated(keep="last")]

    return df


# ============================================================
# index repair
# ============================================================

def _repair_index(df):

    try:

        if isinstance(df.index, pd.MultiIndex):
            df = df.reset_index(drop=True)

        if df.index.duplicated().any():
            df = df.reset_index(drop=True)

        if not isinstance(df.index, pd.RangeIndex):
            df = df.reset_index(drop=True)

    except Exception:
        logger.exception("[FEATURE ENGINE] index repair failed")

    return df


# ============================================================
# price alias repair
# ============================================================

def _repair_price_alias(df: pd.DataFrame) -> pd.DataFrame:

    alias_map = {
        "close_price": "close",
        "open_price": "open",
        "high_price": "high",
        "low_price": "low",
    }

    for src, dst in alias_map.items():

        if src in df.columns and dst not in df.columns:

            try:
                df[dst] = df[src]
            except Exception:
                pass

    return df


# ============================================================
# OHLC保証
# ============================================================

def _ensure_ohlc(df):

    for col in ["open","high","low","close"]:

        if col not in df.columns:
            df[col] = 0.0

    if "volume" not in df.columns:
        df["volume"] = 0.0

    return df


# ============================================================
# safe numeric
# ============================================================

def _safe_numeric(df: pd.DataFrame, col: str, default=0.0):

    if col not in df.columns:
        return pd.Series(default, index=df.index)

    s = df[col]

    try:

        if isinstance(s, pd.DataFrame):
            s = s.iloc[:,0]

        s = pd.to_numeric(s, errors="coerce")

        s = s.replace([np.inf, -np.inf], np.nan)

        s = s.fillna(default)

        return s.astype("float64")

    except Exception:

        return pd.Series(default, index=df.index)


# ============================================================
# symbol sort
# ============================================================

def _sort_symbol_time(df):

    if {"symbol","datetime"}.issubset(df.columns):

        try:

            df["symbol"] = df["symbol"].astype(str)

            df = df.sort_values(
                ["symbol","datetime"],
                kind="mergesort"
            )

        except Exception:
            pass

    return df


# ============================================================
# moving average
# ============================================================

def _add_ma(df):

    close = _safe_numeric(df,"close")

    if "symbol" in df.columns:

        g = close.groupby(df["symbol"])

        df["ma5"] = g.transform(lambda x: x.rolling(5,min_periods=1).mean())
        df["ma10"] = g.transform(lambda x: x.rolling(10,min_periods=1).mean())
        df["ma25"] = g.transform(lambda x: x.rolling(25,min_periods=1).mean())
        df["ma75"] = g.transform(lambda x: x.rolling(75,min_periods=1).mean())

    else:

        df["ma5"] = close.rolling(5,min_periods=1).mean()
        df["ma10"] = close.rolling(10,min_periods=1).mean()
        df["ma25"] = close.rolling(25,min_periods=1).mean()
        df["ma75"] = close.rolling(75,min_periods=1).mean()

    return df


# ============================================================
# slope
# ============================================================

def _add_slope(df):

    for col in ["ma5","ma10","ma25","ma75"]:

        if col not in df.columns:
            continue

        s = _safe_numeric(df,col)

        if "symbol" in df.columns:
            df[f"{col}_slope"] = s.groupby(df["symbol"]).diff()
        else:
            df[f"{col}_slope"] = s.diff()

    if "ma5_slope" in df.columns:
        df["score_slope"] = df["ma5_slope"]

    return df


# ============================================================
# returns
# ============================================================

def _add_returns(df):

    close = _safe_numeric(df,"close")

    if "symbol" in df.columns:

        g = close.groupby(df["symbol"])

        df["ret1"] = g.pct_change(1)
        df["ret3"] = g.pct_change(3)
        df["ret5"] = g.pct_change(5)

    else:

        df["ret1"] = close.pct_change(1)
        df["ret3"] = close.pct_change(3)
        df["ret5"] = close.pct_change(5)

    return df


# ============================================================
# volatility
# ============================================================

def _add_volatility(df):

    close = _safe_numeric(df,"close")

    if "symbol" in df.columns:

        ret = close.groupby(df["symbol"]).pct_change()

        df["volatility_5"] = (
            ret.groupby(df["symbol"])
            .rolling(5,min_periods=1)
            .std()
            .reset_index(level=0,drop=True)
        )

        df["volatility_10"] = (
            ret.groupby(df["symbol"])
            .rolling(10,min_periods=1)
            .std()
            .reset_index(level=0,drop=True)
        )

    else:

        ret = close.pct_change()

        df["volatility_5"] = ret.rolling(5,min_periods=1).std()
        df["volatility_10"] = ret.rolling(10,min_periods=1).std()

    return df


# ============================================================
# volume features
# ============================================================

def _add_volume_features(df):

    volume = _safe_numeric(df,"volume")

    if "symbol" in df.columns:

        vol_ma = (
            volume.groupby(df["symbol"])
            .rolling(20,min_periods=1)
            .mean()
            .reset_index(level=0,drop=True)
        )

        vol_std = (
            volume.groupby(df["symbol"])
            .rolling(20,min_periods=1)
            .std()
            .reset_index(level=0,drop=True)
        )

    else:

        vol_ma = volume.rolling(20,min_periods=1).mean()
        vol_std = volume.rolling(20,min_periods=1).std()

    df["volume_ratio"] = volume / (vol_ma + 1)
    df["volume_zscore"] = (volume - vol_ma) / (vol_std + 1)

    return df


# ============================================================
# VWAP deviation
# ============================================================

def _add_vwap_deviation(df):

    if "vwap" not in df.columns:
        return df

    close = _safe_numeric(df,"close")
    vwap = _safe_numeric(df,"vwap")

    df["vwap_dev"] = (close - vwap) / (vwap + 1e-9)

    return df


# ============================================================
# ranking features
# ============================================================

def _add_ranking_features(df):

    if "ranking_score" not in df.columns:
        return df

    r = _safe_numeric(df,"ranking_score")

    try:

        if "symbol" in df.columns:

            df["ranking_velocity"] = r.groupby(df["symbol"]).diff()

            df["ranking_strength"] = (
                r.groupby(df["symbol"])
                .rolling(5,min_periods=1)
                .mean()
                .reset_index(level=0,drop=True)
            )

        else:

            df["ranking_velocity"] = r.diff()
            df["ranking_strength"] = r.rolling(5,min_periods=1).mean()

    except Exception:

        df["ranking_velocity"] = r.diff()
        df["ranking_strength"] = r.rolling(5,min_periods=1).mean()

    return df


# ============================================================
# sanitize numeric
# ============================================================

def _sanitize(df):

    try:

        df = df.replace([np.inf,-np.inf],np.nan)

        numeric_cols = df.select_dtypes(include=[np.number]).columns

        df[numeric_cols] = df[numeric_cols].fillna(0)

    except Exception:
        pass

    return df


# ============================================================
# core feature builder
# ============================================================

def build_features(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or len(df)==0:
        return df

    try:

        df = _remove_duplicate_columns(df)

        df = _sanitize_dataframe(df.copy())

        df = _repair_index(df)

        df = _repair_price_alias(df)

        df = _ensure_ohlc(df)

        df = _sort_symbol_time(df)

        df = _add_ma(df)

        df = _add_slope(df)

        df = _add_returns(df)

        df = _add_volatility(df)

        df = _add_volume_features(df)

        df = _add_vwap_deviation(df)

        df = _add_ranking_features(df)

        df = _sanitize(df)

    except Exception:

        logger.exception("[FEATURE ENGINE] failed")

    return df


# ============================================================
# MA generation guard
# ============================================================

def _ensure_ma(df: pd.DataFrame) -> pd.DataFrame:

    if {"ma5","ma25","ma75"}.issubset(df.columns):
        return df

    if "close" not in df.columns:
        return df

    try:

        if "symbol" in df.columns:

            df["ma5"] = (
                df.groupby("symbol")["close"]
                .transform(lambda x: x.rolling(5,min_periods=1).mean())
            )

            df["ma25"] = (
                df.groupby("symbol")["close"]
                .transform(lambda x: x.rolling(25,min_periods=1).mean())
            )

            df["ma75"] = (
                df.groupby("symbol")["close"]
                .transform(lambda x: x.rolling(75,min_periods=1).mean())
            )

        else:

            df["ma5"] = df["close"].rolling(5,min_periods=1).mean()
            df["ma25"] = df["close"].rolling(25,min_periods=1).mean()
            df["ma75"] = df["close"].rolling(75,min_periods=1).mean()

    except Exception:
        logger.exception("[FEATURE ENGINE] MA generation failed")

    return df


# ============================================================
# PUBLIC API
# ============================================================

def run_feature_engine(df: pd.DataFrame) -> pd.DataFrame:

    try:

        df = build_features(df)

        df = _ensure_ma(df)

        return df

    except Exception:

        logger.exception("[FEATURE ENGINE] run_feature_engine failed")

        return df