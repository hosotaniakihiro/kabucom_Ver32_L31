# ============================================================
# File: trading/entry/entry_timing_engine.py
# Ver1.0-INSTITUTIONAL-ENTRY-TIMING
# ------------------------------------------------------------
# ✔ entry timing detection
# ✔ breakout timing
# ✔ volume ignition
# ✔ VWAP reclaim
# ✔ momentum continuation
# ✔ NaN safe
# ✔ vectorized
# ============================================================

from __future__ import annotations

import logging
import pandas as pd
import numpy as np

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
# breakout score
# ============================================================

def _breakout_score(df):

    close = _safe(df, "close")
    high = _safe(df, "high")

    prev_high = high.shift(1)

    df["entry_breakout"] = (close > prev_high).astype(int)

    return df


# ============================================================
# VWAP reclaim
# ============================================================

def _vwap_reclaim(df):

    close = _safe(df, "close")
    vwap = _safe(df, "vwap")

    df["entry_vwap_reclaim"] = (close > vwap).astype(int)

    return df


# ============================================================
# volume ignition
# ============================================================

def _volume_ignition(df):

    volume = _safe(df, "volume")

    vol_ma = volume.rolling(10).mean()

    df["entry_volume_spike"] = volume / (vol_ma + 1e-9)

    return df


# ============================================================
# momentum continuation
# ============================================================

def _momentum(df):

    close = _safe(df, "close")

    df["entry_momentum"] = close.diff()

    return df


# ============================================================
# entry timing score
# ============================================================

def _entry_score(df):

    df["entry_timing_score"] = (
        df["entry_breakout"] * 30
        + df["entry_vwap_reclaim"] * 20
        + df["entry_volume_spike"] * 25
        + df["entry_momentum"] * 10
    )

    return df


# ============================================================
# main
# ============================================================

def apply_entry_timing_engine(df: pd.DataFrame) -> pd.DataFrame:

    try:

        if df is None or df.empty:
            return df

        df = df.copy()

        df = _breakout_score(df)

        df = _vwap_reclaim(df)

        df = _volume_ignition(df)

        df = _momentum(df)

        df = _entry_score(df)

        return df

    except Exception:

        logger.exception("[entry_timing_engine] failed")

        return df