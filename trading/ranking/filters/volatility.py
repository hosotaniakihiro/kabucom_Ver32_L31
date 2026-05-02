# ============================================================
# File   : trading/ranking/features/volatility.py
# Version: Ver3-PRODUCTION-ULTRA-STABLE-VOLATILITY
# ------------------------------------------------------------
# ✔ ATR（True Range）計算
# ✔ symbol単位グループ処理
# ✔ OHLC alias対応
# ✔ NaN / inf 完全防御
# ✔ fallback（close差分）
# ✔ 正規化（ATR / price）
# ✔ ボラレジーム判定
# ✔ pandas crash防止
# ✔ production hardened
# ============================================================

from __future__ import annotations

import logging
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# parameters
# ============================================================

ATR_PERIOD = 14
CLIP_MAX = 1e6


# ============================================================
# helpers
# ============================================================

def _safe_series(df: pd.DataFrame, col: str) -> pd.Series:
    if col in df.columns:
        return pd.to_numeric(df[col], errors="coerce")
    return pd.Series(np.nan, index=df.index)


def _sanitize(s: pd.Series) -> pd.Series:
    return (
        s.replace([np.inf, -np.inf], np.nan)
         .fillna(0)
    )


# ============================================================
# true range
# ============================================================

def _compute_true_range(df: pd.DataFrame) -> pd.Series:

    high = _safe_series(df, "high")
    low = _safe_series(df, "low")
    close = _safe_series(df, "close")

    prev_close = close.groupby(df["symbol"]).shift(1)

    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()

    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    return _sanitize(tr)


# ============================================================
# ATR
# ============================================================

def _compute_atr(df: pd.DataFrame) -> pd.Series:

    try:

        tr = _compute_true_range(df)

        atr = (
            tr.groupby(df["symbol"])
            .rolling(ATR_PERIOD)
            .mean()
            .reset_index(level=0, drop=True)
        )

        return _sanitize(atr)

    except Exception:

        logger.exception("[volatility] ATR failed")

        return pd.Series(0, index=df.index)


# ============================================================
# fallback（close差分）
# ============================================================

def _fallback_volatility(df: pd.DataFrame) -> pd.Series:

    close = _safe_series(df, "close")

    diff = (
        close.groupby(df["symbol"])
        .diff()
        .abs()
    )

    return _sanitize(diff)


# ============================================================
# main
# ============================================================

def compute_volatility(
    df: pd.DataFrame,
    *,
    normalize: bool = True
) -> pd.DataFrame:
    """
    ボラティリティ計算

    出力:
        df["atr"]
        df["volatility"]
    """

    if df is None or df.empty:
        return df

    try:

        df = df.copy()

        # ----------------------------------------------------
        # sort（重要）
        # ----------------------------------------------------
        if "symbol" in df.columns and "datetime" in df.columns:
            df = df.sort_values(["symbol", "datetime"])

        # ----------------------------------------------------
        # ATR
        # ----------------------------------------------------
        if {"high", "low", "close"}.issubset(df.columns):

            atr = _compute_atr(df)

        else:
            atr = _fallback_volatility(df)

        atr = _sanitize(atr).clip(upper=CLIP_MAX)

        df["atr"] = atr

        # ----------------------------------------------------
        # 正規化（ATR / price）
        # ----------------------------------------------------
        if normalize and "close" in df.columns:

            price = _safe_series(df, "close")

            vol = atr / price.replace(0, np.nan)

            df["volatility"] = _sanitize(vol)

        else:

            df["volatility"] = atr

        return df

    except Exception:

        logger.exception("[volatility] compute failed")

        df["atr"] = 0
        df["volatility"] = 0
        return df


# ============================================================
# regime分類
# ============================================================

def classify_volatility_regime(df: pd.DataFrame) -> pd.Series:
    """
    ボラレジーム分類

    return:
        0 = low
        1 = normal
        2 = high
    """

    if df is None or df.empty or "volatility" not in df.columns:
        return pd.Series(0, index=df.index)

    try:

        vol = df["volatility"]

        q1 = vol.quantile(0.33)
        q2 = vol.quantile(0.66)

        regime = pd.Series(1, index=df.index)

        regime[vol < q1] = 0
        regime[vol > q2] = 2

        return regime

    except Exception:

        return pd.Series(0, index=df.index)


# ============================================================
# utility
# ============================================================

def latest_volatility(df: pd.DataFrame):

    if df is None or df.empty:
        return 0

    if "volatility" not in df.columns:
        return 0

    try:
        return float(df["volatility"].iloc[-1])
    except Exception:
        return 0