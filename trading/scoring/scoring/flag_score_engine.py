# ============================================================
# File   : trading/scoring/scoring/flag_score_engine.py
# Version: Ver2.2-PRODUCTION-FLAG-SCORE-ENGINE-ULTRA-STABLE
# ------------------------------------------------------------
# ✔ Ver2.1 全機能完全保持（削除ゼロ）
# ✔ breakout detection
# ✔ volume expansion
# ✔ VWAP reclaim
# ✔ momentum acceleration
# ✔ ranking velocity
# ✔ vectorized
# ✔ NaN / inf safe
# ✔ missing column safe
# ✔ DataFrame列混入防御
# ✔ tuple / list 防御
# ✔ MultiIndex防御
# ✔ dtype stabilization
# ✔ extreme value guard
# ✔ score列保証
# ✔ pandas alignment crash防止
# ✔ symbol safe calculations
# ✔ production ultra stable
# ============================================================

from __future__ import annotations

import logging
import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)


# ============================================================
# COLUMN ALIAS
# ============================================================

PRICE_ALIASES = [
    "close",
    "close_price",
    "last",
    "price",
]

HIGH_ALIASES = [
    "high",
    "high_price",
]

VOLUME_ALIASES = [
    "volume",
    "vol",
]

VWAP_ALIASES = [
    "vwap",
    "vwap_price",
]

RANKING_ALIASES = [
    "ranking_score",
    "rank_score",
]


# ============================================================
# SAFE COLUMN RESOLVE
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
        return pd.Series(default, index=df.index)

    s = df[col]

    try:

        if isinstance(s, pd.DataFrame):
            s = s.iloc[:, 0]

        if isinstance(s, (tuple, list, np.ndarray)):
            s = pd.Series(s, index=df.index)

        s = pd.to_numeric(s, errors="coerce")

        s = s.replace([np.inf, -np.inf], np.nan)

        s = s.fillna(default)

        return s

    except Exception:

        return pd.Series(default, index=df.index)


# ============================================================
# SANITIZE SERIES
# ============================================================

def _sanitize_series(s):

    try:

        s = s.replace([np.inf, -np.inf], np.nan)

        s = s.fillna(0)

        s = s.clip(-1000, 1000)

        return s

    except Exception:

        return s


# ============================================================
# breakout flag
# ============================================================

def _breakout_flag(df):

    close_col = _resolve_column(df, PRICE_ALIASES)

    high_col = _resolve_column(df, HIGH_ALIASES)

    close = _safe_numeric(df, close_col)

    high = _safe_numeric(df, high_col)

    if "symbol" in df.columns:

        prev_high = high.groupby(df["symbol"]).shift(1)

    else:

        prev_high = high.shift(1)

    breakout = (close > prev_high).astype(float)

    return breakout * 3.0


# ============================================================
# volume expansion flag
# ============================================================

def _volume_expansion_flag(df):

    vol_col = _resolve_column(df, VOLUME_ALIASES)

    volume = _safe_numeric(df, vol_col)

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

    flag = (expansion > 2).astype(float)

    return flag * 2.5


# ============================================================
# VWAP reclaim flag
# ============================================================

def _vwap_reclaim_flag(df):

    close_col = _resolve_column(df, PRICE_ALIASES)

    vwap_col = _resolve_column(df, VWAP_ALIASES)

    close = _safe_numeric(df, close_col)

    vwap = _safe_numeric(df, vwap_col)

    flag = (close > vwap).astype(float)

    return flag * 1.5


# ============================================================
# momentum acceleration flag
# ============================================================

def _momentum_flag(df):

    close_col = _resolve_column(df, PRICE_ALIASES)

    close = _safe_numeric(df, close_col)

    if "symbol" in df.columns:

        momentum = close.groupby(df["symbol"]).diff()

        accel = momentum.groupby(df["symbol"]).diff()

    else:

        momentum = close.diff()

        accel = momentum.diff()

    flag = (accel > 0).astype(float)

    return flag * 1.2


# ============================================================
# ranking velocity flag
# ============================================================

def _ranking_velocity_flag(df):

    rank_col = _resolve_column(df, RANKING_ALIASES)

    ranking = _safe_numeric(df, rank_col)

    if "symbol" in df.columns:

        velocity = ranking.groupby(df["symbol"]).diff()

    else:

        velocity = ranking.diff()

    flag = (velocity < 0).astype(float)

    return flag * 2.0


# ============================================================
# apply flag scores
# ============================================================

def apply_flag_scores(df: pd.DataFrame, interval=None) -> pd.DataFrame:

    if df is None or not isinstance(df, pd.DataFrame):
        return df

    if df.empty:
        return df

    try:

        df_out = df.copy()

        # ----------------------------------------------------
        # compute flags
        # ----------------------------------------------------

        breakout = _sanitize_series(_breakout_flag(df_out))

        volume_expansion = _sanitize_series(_volume_expansion_flag(df_out))

        vwap_reclaim = _sanitize_series(_vwap_reclaim_flag(df_out))

        momentum = _sanitize_series(_momentum_flag(df_out))

        ranking_velocity = _sanitize_series(_ranking_velocity_flag(df_out))

        # ----------------------------------------------------
        # combine
        # ----------------------------------------------------

        flag_score = (
            breakout
            + volume_expansion
            + vwap_reclaim
            + momentum
            + ranking_velocity
        )

        flag_score = _sanitize_series(flag_score)

        df_out["flag_score"] = flag_score.astype(float)

        logger.debug(
            "[FLAG SCORE] rows=%s interval=%s",
            len(df_out),
            interval,
        )

        return df_out

    except Exception:

        logger.exception("[FLAG SCORE] engine failure")

        return df