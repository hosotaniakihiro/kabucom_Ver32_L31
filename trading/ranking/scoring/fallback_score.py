# ============================================================
# File   : trading/ranking/scoring/fallback_score.py
# Version: Ver1.1-PRODUCTION-FALLBACK-RANKING-SCORE-FIXED
# ------------------------------------------------------------
# ✔ fallback ranking score
# ✔ symbol grouped calculation
# ✔ momentum
# ✔ volume spike detection
# ✔ VWAP breakout
# ✔ MA slope
# ✔ NaN / inf guard
# ✔ pandas vectorized
# ✔ production safe
# ✔ NEW: stable sort by symbol/datetime before groupby calc
# ✔ NEW: display column normalization helper for ranking summary
# ✔ NEW: fallback display columns guarantee
# ============================================================

from __future__ import annotations

import logging
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# score weights
# ============================================================

MOMENTUM_WEIGHT = 50
VOLUME_WEIGHT = 20
VWAP_WEIGHT = 30
SLOPE_WEIGHT = 10


# ============================================================
# helpers
# ============================================================

def _sanitize_numeric(series: pd.Series) -> pd.Series:
    return (
        pd.to_numeric(series, errors="coerce")
        .replace([np.inf, -np.inf], 0)
        .fillna(0)
    )


def _ensure_symbol(df: pd.DataFrame) -> bool:
    if "symbol" not in df.columns:
        logger.warning("[fallback_score] symbol column missing")
        return False
    return True


def _stable_sort(df: pd.DataFrame) -> pd.DataFrame:
    """
    groupby + diff / pct_change / rolling が時系列順で計算されるように安定ソート
    """
    if df is None or df.empty:
        return df

    try:
        out = df.copy()

        if "symbol" not in out.columns:
            return out

        if "datetime" in out.columns:
            out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
            out = out.sort_values(["symbol", "datetime"], kind="stable")
        else:
            out = out.sort_values(["symbol"], kind="stable")

        return out.reset_index(drop=True)

    except Exception:
        logger.exception("[fallback_score] stable sort failed")
        return df.copy()


def _coalesce_first(df: pd.DataFrame, columns: list[str], default=0) -> pd.Series:
    for col in columns:
        if col in df.columns:
            return df[col]
    return pd.Series(default, index=df.index)


# ============================================================
# momentum
# ============================================================

def _momentum(close: pd.Series, symbols: pd.Series):
    return (
        _sanitize_numeric(close)
        .groupby(symbols)
        .pct_change()
        .replace([np.inf, -np.inf], 0)
        .fillna(0)
    )


# ============================================================
# volume spike
# ============================================================

def _volume_ratio(volume: pd.Series, symbols: pd.Series):
    volume = _sanitize_numeric(volume)

    vol_ma = (
        volume
        .groupby(symbols)
        .rolling(5, min_periods=1)
        .mean()
        .reset_index(level=0, drop=True)
    )

    ratio = volume / vol_ma.replace(0, np.nan)

    return ratio.replace([np.inf, -np.inf], 0).fillna(0)


# ============================================================
# vwap score
# ============================================================

def _vwap_score(df: pd.DataFrame):
    if "vwap" not in df.columns:
        return pd.Series(0, index=df.index)

    close = _sanitize_numeric(df.get("close", pd.Series(0, index=df.index)))
    vwap = _sanitize_numeric(df["vwap"]).replace(0, np.nan)

    score = (close - vwap) / vwap

    return score.replace([np.inf, -np.inf], 0).fillna(0)


# ============================================================
# slope score
# ============================================================

def _ma_slope(close: pd.Series, symbols: pd.Series):
    close = _sanitize_numeric(close)

    ma25 = (
        close
        .groupby(symbols)
        .rolling(25, min_periods=1)
        .mean()
        .reset_index(level=0, drop=True)
    )

    slope = (
        ma25
        .groupby(symbols)
        .diff()
        .replace([np.inf, -np.inf], 0)
        .fillna(0)
    )

    return slope


# ============================================================
# ranking summary display helpers
# ============================================================

def ensure_fallback_display_columns(df: pd.DataFrame) -> pd.DataFrame:
    """
    ranking summary 表示側が期待する列を保証する。
    fallback経路でも '-' だらけになりにくくする。
    """
    if df is None:
        return pd.DataFrame()

    if not isinstance(df, pd.DataFrame):
        return pd.DataFrame()

    if df.empty:
        return df.copy()

    out = df.copy()

    alias_map = {
        "close_price": "close",
        "last_price": "close",
        "current_price": "close",
        "price": "close",
        "score_slope": "slope",
        "ma25_slope": "slope",
        "slope_atr_scaled": "slope",
        "rsi14": "rsi",
        "macd_value": "macd",
        "macd_hist": "hist",
        "rank": "best_rank",
        "best": "best_rank",
        "rank_type_name": "rank_type",
        "type": "rank_type",
        "hist_count": "hist",
    }

    for src, dst in alias_map.items():
        if src in out.columns and dst not in out.columns:
            out[dst] = out[src]

    if "close" not in out.columns:
        out["close"] = _coalesce_first(out, ["close_price", "last_price", "current_price", "price"], default=0)

    if "slope" not in out.columns:
        if "symbol" in out.columns:
            out["slope"] = _ma_slope(_coalesce_first(out, ["close"], default=0), out["symbol"])
        else:
            out["slope"] = 0.0

    if "rsi" not in out.columns:
        out["rsi"] = np.nan

    if "macd" not in out.columns:
        out["macd"] = np.nan

    if "best_rank" not in out.columns:
        out["best_rank"] = np.nan

    if "rank_type" not in out.columns:
        out["rank_type"] = ""

    if "hist" not in out.columns:
        if "symbol" in out.columns:
            out["hist"] = out.groupby("symbol")["symbol"].transform("size")
        else:
            out["hist"] = 1

    if "score" not in out.columns:
        out["score"] = 0.0

    if "symbolname" not in out.columns:
        out["symbolname"] = out.get("symbol", "")

    # sanitize numeric view columns
    for col in ["close", "score", "slope", "rsi", "macd", "best_rank", "hist"]:
        if col in out.columns:
            out[col] = _sanitize_numeric(out[col])

    return out


# ============================================================
# main fallback score
# ============================================================

def calculate_fallback_score(
    df: pd.DataFrame
) -> tuple[pd.Series, pd.Series]:
    """
    Calculate fallback ranking score.

    Returns
    -------
    score : Series
    slope : Series
    """

    if df is None:
        return pd.Series(dtype=float), pd.Series(dtype=float)

    if not isinstance(df, pd.DataFrame):
        return pd.Series(dtype=float), pd.Series(dtype=float)

    if df.empty:
        return pd.Series(dtype=float), pd.Series(dtype=float)

    try:
        if not _ensure_symbol(df):
            zeros = pd.Series(0, index=df.index)
            return zeros, zeros

        df = _stable_sort(df.copy())

        symbols = df["symbol"]
        close = _coalesce_first(df, ["close", "close_price", "last_price", "current_price", "price"], default=0)
        volume = _coalesce_first(df, ["volume", "trading_volume"], default=0)

        # ----------------------------------------------------
        # momentum
        # ----------------------------------------------------
        momentum = _momentum(close, symbols)

        # ----------------------------------------------------
        # volume spike
        # ----------------------------------------------------
        volume_ratio = _volume_ratio(volume, symbols)

        # ----------------------------------------------------
        # VWAP deviation
        # ----------------------------------------------------
        vwap_score = _vwap_score(df)

        # ----------------------------------------------------
        # slope
        # ----------------------------------------------------
        slope = _ma_slope(close, symbols)

        # ----------------------------------------------------
        # final score
        # ----------------------------------------------------
        score = (
            momentum * MOMENTUM_WEIGHT
            + volume_ratio * VOLUME_WEIGHT
            + vwap_score * VWAP_WEIGHT
            + slope * SLOPE_WEIGHT
        )

        score = _sanitize_numeric(score)
        slope = _sanitize_numeric(slope)

        return score, slope

    except Exception:
        logger.exception("[fallback_score] failed")
        zeros = pd.Series(0, index=df.index)
        return zeros, zeros
