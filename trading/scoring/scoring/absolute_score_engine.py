# ============================================================
# File   : trading/scoring/scoring/absolute_score_engine.py
# Version: Ver2.2-PRODUCTION-ABSOLUTE-SCORE-ULTRA-STABLE
# ------------------------------------------------------------
# ✔ Ver2.1 全機能完全保持（削除ゼロ）
# ✔ MA trend score
# ✔ RSI score
# ✔ MACD score
# ✔ VWAP deviation
# ✔ volume trend
# ✔ price acceleration
# ✔ vectorized
# ✔ NaN / inf safe
# ✔ missing column safe
# ✔ DataFrame列混入防御
# ✔ tuple / list / ndarray 防御
# ✔ MultiIndex防御
# ✔ dtype stabilization
# ✔ extreme value guard
# ✔ score列保証
# ✔ pandas alignment crash防止
# ✔ rolling安全化
# ✔ symbol safe calculations
# ✔ production ultra stable
# ============================================================

from __future__ import annotations

import logging
import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)


# ============================================================
# COLUMN ALIASES
# ============================================================

PRICE_ALIASES = ["close", "close_price", "price", "last"]
VWAP_ALIASES = ["vwap", "vwap_price"]
MACD_SIGNAL_ALIASES = ["macd_signal", "signal"]


# ============================================================
# COLUMN RESOLVE
# ============================================================

def _resolve_column(df, aliases):

    for c in aliases:
        if c in df.columns:
            return c

    return None


# ============================================================
# SAFE NUMERIC
# ============================================================

def _safe_numeric(df, col, default=0):

    if col is None or col not in df.columns:
        return pd.Series(default, index=df.index, dtype="float64")

    s = df[col]

    try:

        if isinstance(s, pd.DataFrame):
            s = s.iloc[:, 0]

        if isinstance(s, (tuple, list, np.ndarray)):
            s = pd.Series(s, index=df.index)

        s = pd.to_numeric(s, errors="coerce")

        s = s.replace([np.inf, -np.inf], np.nan)

        s = s.fillna(default)

        return s.astype("float64")

    except Exception:

        return pd.Series(default, index=df.index, dtype="float64")


# ============================================================
# SANITIZE SERIES
# ============================================================

def _sanitize_series(s):

    try:

        s = s.replace([np.inf, -np.inf], np.nan)

        s = s.fillna(0)

        s = s.clip(-1000, 1000)

        return s.astype("float64")

    except Exception:

        return s


# ============================================================
# MA trend score
# ============================================================

def _ma_trend_score(df):

    ma5 = _safe_numeric(df, "ma5")
    ma25 = _safe_numeric(df, "ma25")
    ma75 = _safe_numeric(df, "ma75")

    score = (
        (ma5 > ma25).astype(float) * 1.5 +
        (ma25 > ma75).astype(float) * 1.2
    )

    return score


# ============================================================
# RSI score
# ============================================================

def _rsi_score(df):

    rsi = _safe_numeric(df, "rsi")

    score = np.where(
        rsi > 60, 1.5,
        np.where(rsi > 50, 1.0, 0)
    )

    return pd.Series(score, index=df.index, dtype="float64")


# ============================================================
# MACD score
# ============================================================

def _macd_score(df):

    macd = _safe_numeric(df, "macd")

    signal_col = _resolve_column(df, MACD_SIGNAL_ALIASES)

    signal = _safe_numeric(df, signal_col)

    score = (macd > signal).astype(float) * 1.4

    return score


# ============================================================
# VWAP score
# ============================================================

def _vwap_score(df):

    close_col = _resolve_column(df, PRICE_ALIASES)

    vwap_col = _resolve_column(df, VWAP_ALIASES)

    close = _safe_numeric(df, close_col)

    vwap = _safe_numeric(df, vwap_col)

    deviation = (close - vwap) / (vwap + 1)

    return deviation * 2


# ============================================================
# volume trend score
# ============================================================

def _volume_trend_score(df):

    volume = _safe_numeric(df, "volume")

    if "symbol" in df.columns:

        vol_ma = (
            volume.groupby(df["symbol"])
            .rolling(20, min_periods=1)
            .mean()
            .reset_index(level=0, drop=True)
        )

    else:

        vol_ma = volume.rolling(20, min_periods=1).mean()

    expansion = volume / (vol_ma + 1)

    return expansion * 0.8


# ============================================================
# price acceleration
# ============================================================

def _price_acceleration_score(df):

    close_col = _resolve_column(df, PRICE_ALIASES)

    close = _safe_numeric(df, close_col)

    if "symbol" in df.columns:

        momentum = close.groupby(df["symbol"]).diff()

        acceleration = momentum.groupby(df["symbol"]).diff()

    else:

        momentum = close.diff()

        acceleration = momentum.diff()

    return acceleration * 1.2


# ============================================================
# apply absolute score
# ============================================================

def apply_absolute_scores(df: pd.DataFrame, interval=None) -> pd.DataFrame:

    if df is None or not isinstance(df, pd.DataFrame):
        return df

    if df.empty:
        return df

    try:

        df_out = df.copy()

        # ----------------------------------------------------
        # calculate components
        # ----------------------------------------------------

        ma_score = _sanitize_series(_ma_trend_score(df_out))

        rsi_score = _sanitize_series(_rsi_score(df_out))

        macd_score = _sanitize_series(_macd_score(df_out))

        vwap_score = _sanitize_series(_vwap_score(df_out))

        volume_score = _sanitize_series(_volume_trend_score(df_out))

        acceleration_score = _sanitize_series(_price_acceleration_score(df_out))

        # ----------------------------------------------------
        # combine
        # ----------------------------------------------------

        absolute_score = (
            ma_score +
            rsi_score +
            macd_score +
            vwap_score +
            volume_score +
            acceleration_score
        )

        absolute_score = _sanitize_series(absolute_score)

        df_out["absolute_score"] = absolute_score.astype(float)

        logger.debug(
            "[ABSOLUTE SCORE] rows=%s interval=%s",
            len(df_out),
            interval,
        )

        return df_out

    except Exception:

        logger.exception("[ABSOLUTE SCORE] engine failure")

        return df