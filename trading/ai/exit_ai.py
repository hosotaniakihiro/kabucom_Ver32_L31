# ============================================================
# File   : trading/ai/exit_ai.py
# Version: Ver1.0-PRO-INSTITUTIONAL-EXIT-AI
# ------------------------------------------------------------
# ✔ profit taking AI
# ✔ stop loss AI
# ✔ blowoff detection
# ✔ VWAP loss
# ✔ momentum loss
# ✔ trailing stop
# ✔ NaN / inf safe
# ✔ production ready
# ============================================================

from __future__ import annotations

import logging
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# safe numeric
# ============================================================

def _safe(df, col, default=0):

    if col not in df.columns:
        return pd.Series(default, index=df.index)

    s = pd.to_numeric(df[col], errors="coerce")

    s = s.replace([np.inf, -np.inf], np.nan)

    return s.fillna(default)


# ============================================================
# profit %
# ============================================================

def _profit_pct(df):

    entry = _safe(df, "entry_price")
    close = _safe(df, "close")

    df["profit_pct"] = (close - entry) / (entry + 1e-9)

    return df


# ============================================================
# momentum loss
# ============================================================

def _momentum_loss(df):

    close = _safe(df, "close")

    df["exit_momentum"] = close.diff()

    return df


# ============================================================
# VWAP loss
# ============================================================

def _vwap_loss(df):

    close = _safe(df, "close")
    vwap = _safe(df, "vwap")

    df["exit_vwap_loss"] = (close < vwap).astype(int)

    return df


# ============================================================
# volume exhaustion
# ============================================================

def _volume_exhaust(df):

    volume = _safe(df, "volume")

    vol_ma = volume.rolling(10).mean()

    df["exit_volume_exhaust"] = volume / (vol_ma + 1e-9)

    return df


# ============================================================
# trailing stop
# ============================================================

def _trailing_stop(df):

    high = _safe(df, "high")
    close = _safe(df, "close")

    rolling_high = high.rolling(10).max()

    df["exit_trailing"] = (rolling_high - close) / (rolling_high + 1e-9)

    return df


# ============================================================
# exit score
# ============================================================

def _exit_score(df):

    df["exit_score"] = (
        df["profit_pct"] * 50
        - df["exit_momentum"] * 10
        + df["exit_vwap_loss"] * 20
        + df["exit_volume_exhaust"] * 5
        + df["exit_trailing"] * 30
    )

    return df


# ============================================================
# main engine
# ============================================================

def apply_exit_ai(df: pd.DataFrame) -> pd.DataFrame:

    try:

        if df is None or df.empty:
            return df

        df = df.copy()

        df = _profit_pct(df)

        df = _momentum_loss(df)

        df = _vwap_loss(df)

        df = _volume_exhaust(df)

        df = _trailing_stop(df)

        df = _exit_score(df)

        return df

    except Exception:

        logger.exception("[exit_ai] failed")

        return df


# ============================================================
# exit decision
# ============================================================

def detect_exit_signal(df: pd.DataFrame) -> pd.DataFrame:

    try:

        if df is None or df.empty:
            return pd.DataFrame()

        df = apply_exit_ai(df)

        cond = (
            (df["profit_pct"] > 0.05)
            | (df["exit_trailing"] > 0.03)
            | (df["exit_vwap_loss"] == 1)
        )

        return df.loc[cond].copy()

    except Exception:

        logger.exception("[exit_ai] detection failed")

        return pd.DataFrame()