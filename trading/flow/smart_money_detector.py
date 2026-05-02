# ============================================================
# File: trading/flow/smart_money_detector.py
# Ver1.0-INSTITUTIONAL-SMART-MONEY-DETECTOR
# ------------------------------------------------------------
# ✔ smart money flow detection
# ✔ volume expansion
# ✔ VWAP accumulation
# ✔ price acceleration
# ✔ ranking velocity
# ✔ liquidity filter
# ✔ vectorized
# ✔ NaN safe
# ============================================================

from __future__ import annotations

import logging
import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)


# ============================================================
# safe numeric
# ============================================================

def _safe_numeric(df, col, default=0):

    if col not in df.columns:
        return pd.Series(default, index=df.index)

    s = pd.to_numeric(df[col], errors="coerce")

    s = s.replace([np.inf, -np.inf], np.nan)

    return s.fillna(default)


# ============================================================
# volume accumulation
# ============================================================

def _volume_accumulation(df):

    volume = _safe_numeric(df, "volume")

    vol_ma = volume.rolling(20).mean()

    df["smart_volume"] = volume / (vol_ma + 1e-9)

    return df


# ============================================================
# vwap accumulation
# ============================================================

def _vwap_accumulation(df):

    if "vwap" not in df.columns:

        df["smart_vwap"] = 0

        return df

    close = _safe_numeric(df, "close")
    vwap = _safe_numeric(df, "vwap")

    df["smart_vwap"] = (close - vwap) / (vwap + 1e-9)

    return df


# ============================================================
# price acceleration
# ============================================================

def _price_accel(df):

    close = _safe_numeric(df, "close")

    df["smart_price_accel"] = close.diff().diff()

    return df


# ============================================================
# ranking velocity
# ============================================================

def _ranking_velocity(df):

    if "rank_velocity" not in df.columns:

        df["smart_rank_velocity"] = 0

        return df

    df["smart_rank_velocity"] = df["rank_velocity"]

    return df


# ============================================================
# liquidity
# ============================================================

def _liquidity(df):

    turnover = _safe_numeric(df, "turnover")

    df["smart_liquidity"] = np.log1p(turnover)

    return df


# ============================================================
# smart money score
# ============================================================

def _smart_money_score(df):

    df["smart_money_score"] = (
        df["smart_volume"] * 30
        + df["smart_vwap"] * 40
        + df["smart_price_accel"] * 10
        + df["smart_rank_velocity"] * 15
        + df["smart_liquidity"] * 5
    )

    return df


# ============================================================
# main detector
# ============================================================

def apply_smart_money_detector(df: pd.DataFrame) -> pd.DataFrame:

    try:

        if df is None or df.empty:
            return df

        df = df.copy()

        df = _volume_accumulation(df)

        df = _vwap_accumulation(df)

        df = _price_accel(df)

        df = _ranking_velocity(df)

        df = _liquidity(df)

        df = _smart_money_score(df)

        return df

    except Exception:

        logger.exception("[smart_money_detector] failed")

        return df