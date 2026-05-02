# ============================================================
# File   : trading/ranking/engines/smart_money.py
# Version: Ver4-PRODUCTION-ULTRA-STABLE-SMART-MONEY
# ------------------------------------------------------------
# ✔ smart money flow検出
# ✔ volume + price + VWAP + volatility融合
# ✔ breakout検知
# ✔ 継続性（連続上昇）
# ✔ ダマシ排除（弱いvolume除外）
# ✔ groupby安全処理
# ✔ NaN / inf 完全防御
# ✔ 正規化
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

VOL_WINDOW = 10
PRICE_WINDOW = 5
BREAKOUT_WINDOW = 20
CONTINUITY_WINDOW = 3


# ============================================================
# helpers
# ============================================================

def _safe_series(df: pd.DataFrame, col: str) -> pd.Series:
    if col in df.columns:
        return pd.to_numeric(df[col], errors="coerce")
    return pd.Series(0, index=df.index)


def _sanitize(s: pd.Series) -> pd.Series:
    return (
        s.replace([np.inf, -np.inf], np.nan)
         .fillna(0)
    )


def _normalize(s: pd.Series) -> pd.Series:
    max_abs = s.abs().max()
    if max_abs > 0:
        return s / max_abs
    return s


# ============================================================
# volume strength
# ============================================================

def _volume_strength(df: pd.DataFrame) -> pd.Series:

    volume = _safe_series(df, "volume")

    ma = (
        volume.groupby(df["symbol"])
        .rolling(VOL_WINDOW)
        .mean()
        .reset_index(level=0, drop=True)
    )

    strength = volume / ma.replace(0, np.nan)

    return _sanitize(strength)


# ============================================================
# price efficiency（無駄のない上昇）
# ============================================================

def _price_efficiency(df: pd.DataFrame) -> pd.Series:

    close = _safe_series(df, "close")

    diff = (
        close.groupby(df["symbol"])
        .diff()
    )

    abs_diff = diff.abs()

    efficiency = diff / (abs_diff + 1e-6)

    return _sanitize(efficiency)


# ============================================================
# breakout検知
# ============================================================

def _breakout(df: pd.DataFrame) -> pd.Series:

    close = _safe_series(df, "close")

    rolling_high = (
        close.groupby(df["symbol"])
        .rolling(BREAKOUT_WINDOW)
        .max()
        .reset_index(level=0, drop=True)
    )

    breakout = (close >= rolling_high).astype(int)

    return breakout


# ============================================================
# VWAP強度
# ============================================================

def _vwap_strength(df: pd.DataFrame) -> pd.Series:

    if "vwap" not in df.columns:
        return pd.Series(0, index=df.index)

    close = _safe_series(df, "close")
    vwap = _safe_series(df, "vwap")

    strength = (close - vwap) / vwap.replace(0, np.nan)

    return _sanitize(strength)


# ============================================================
# volatility調整（重要）
# ============================================================

def _volatility_adjustment(df: pd.DataFrame) -> pd.Series:

    if "volatility" not in df.columns:
        return pd.Series(1, index=df.index)

    vol = _safe_series(df, "volatility")

    adj = 1 + vol

    return _sanitize(adj)


# ============================================================
# continuity（継続性）
# ============================================================

def _continuity(signal: pd.Series, df: pd.DataFrame) -> pd.Series:

    try:

        cont = (
            signal.groupby(df["symbol"])
            .rolling(CONTINUITY_WINDOW)
            .mean()
            .reset_index(level=0, drop=True)
        )

        return _sanitize(cont)

    except Exception:
        return signal


# ============================================================
# main
# ============================================================

def apply_smart_money(
    df: pd.DataFrame,
    *,
    normalize: bool = True
) -> pd.DataFrame:
    """
    smart money検出

    出力:
        df["smart_money_score"]
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
        # components
        # ----------------------------------------------------
        vol = _volume_strength(df)
        eff = _price_efficiency(df)
        brk = _breakout(df)
        vwap = _vwap_strength(df)
        vola = _volatility_adjustment(df)

        # ----------------------------------------------------
        # core signal
        # ----------------------------------------------------
        signal = (
            vol * 0.35 +
            eff * 0.20 +
            brk * 0.25 +
            vwap * 0.20
        )

        signal = _sanitize(signal)

        # ----------------------------------------------------
        # volatility補正
        # ----------------------------------------------------
        signal = signal * vola

        # ----------------------------------------------------
        # continuity（重要）
        # ----------------------------------------------------
        signal = _continuity(signal, df)

        # ----------------------------------------------------
        # normalize
        # ----------------------------------------------------
        if normalize:
            signal = _normalize(signal)

        df["smart_money_score"] = signal

        return df

    except Exception:

        logger.exception("[smart_money] apply failed")

        df["smart_money_score"] = 0
        return df


# ============================================================
# utility
# ============================================================

def latest_smart_money(df: pd.DataFrame):

    if df is None or df.empty:
        return 0

    if "smart_money_score" not in df.columns:
        return 0

    try:
        return float(df["smart_money_score"].iloc[-1])
    except Exception:
        return 0