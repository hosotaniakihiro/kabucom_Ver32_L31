# ============================================================
# File   : trading/ranking/engines/institutional.py
# Version: Ver3-PRODUCTION-ULTRA-STABLE-INSTITUTIONAL
# ------------------------------------------------------------
# ✔ 機関資金フロー検知
# ✔ volume急増 + price上昇の同時検出
# ✔ VWAP乖離
# ✔ 継続性（連続性）評価
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


# ============================================================
# volume spike
# ============================================================

def _volume_spike(df: pd.DataFrame) -> pd.Series:

    volume = _safe_series(df, "volume")

    ma = (
        volume.groupby(df["symbol"])
        .rolling(VOL_WINDOW)
        .mean()
        .reset_index(level=0, drop=True)
    )

    spike = volume / ma.replace(0, np.nan)

    return _sanitize(spike)


# ============================================================
# price momentum
# ============================================================

def _price_momentum(df: pd.DataFrame) -> pd.Series:

    close = _safe_series(df, "close")

    mom = (
        close.groupby(df["symbol"])
        .pct_change(PRICE_WINDOW)
    )

    return _sanitize(mom)


# ============================================================
# VWAP乖離
# ============================================================

def _vwap_deviation(df: pd.DataFrame) -> pd.Series:

    if "vwap" not in df.columns:
        return pd.Series(0, index=df.index)

    close = _safe_series(df, "close")
    vwap = _safe_series(df, "vwap")

    dev = (close - vwap) / vwap.replace(0, np.nan)

    return _sanitize(dev)


# ============================================================
# continuity（継続性）
# ============================================================

def _continuity(signal: pd.Series, df: pd.DataFrame) -> pd.Series:

    try:

        cont = (
            signal.groupby(df["symbol"])
            .rolling(CONTINUITY_WINDOW)
            .sum()
            .reset_index(level=0, drop=True)
        )

        return _sanitize(cont)

    except Exception:
        return signal


# ============================================================
# normalize
# ============================================================

def _normalize(s: pd.Series) -> pd.Series:

    max_abs = s.abs().max()

    if max_abs > 0:
        return s / max_abs

    return s


# ============================================================
# main
# ============================================================

def apply_institutional(
    df: pd.DataFrame,
    *,
    normalize: bool = True
) -> pd.DataFrame:
    """
    機関フロー検知

    出力:
        df["institutional_score"]
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
        vol_spike = _volume_spike(df)
        price_mom = _price_momentum(df)
        vwap_dev = _vwap_deviation(df)

        # ----------------------------------------------------
        # core signal
        # ----------------------------------------------------
        signal = (
            vol_spike * 0.5 +
            price_mom * 0.3 +
            vwap_dev * 0.2
        )

        signal = _sanitize(signal)

        # ----------------------------------------------------
        # continuity boost
        # ----------------------------------------------------
        signal = _continuity(signal, df)

        # ----------------------------------------------------
        # normalize
        # ----------------------------------------------------
        if normalize:
            signal = _normalize(signal)

        df["institutional_score"] = signal

        return df

    except Exception:

        logger.exception("[institutional] apply failed")

        df["institutional_score"] = 0
        return df


# ============================================================
# utility
# ============================================================

def latest_institutional(df: pd.DataFrame):

    if df is None or df.empty:
        return 0

    if "institutional_score" not in df.columns:
        return 0

    try:
        return float(df["institutional_score"].iloc[-1])
    except Exception:
        return 0