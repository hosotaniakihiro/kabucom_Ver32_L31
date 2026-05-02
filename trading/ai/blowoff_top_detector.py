# ============================================================
# File: trading/ai/blowoff_top_detector.py
# Ver1.0-PRO-BLOWOFF-TOP-DETECTOR
# ------------------------------------------------------------
# ✔ blow-off top detection
# ✔ volume climax
# ✔ price acceleration
# ✔ VWAP deviation
# ✔ RSI overheat
# ✔ vectorized
# ✔ NaN安全
# ✔ exit signal
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
# volume climax
# ============================================================

def _volume_climax(df):

    volume = _safe_numeric(df, "volume")

    vol_ma = volume.rolling(20).mean()

    df["volume_climax"] = volume / (vol_ma + 1e-9)

    return df


# ============================================================
# price acceleration
# ============================================================

def _price_accel(df):

    close = _safe_numeric(df, "close")

    df["price_accel"] = close.diff().diff()

    return df


# ============================================================
# vwap deviation
# ============================================================

def _vwap_dev(df):

    if "vwap" not in df.columns:

        df["vwap_dev"] = 0

        return df

    close = _safe_numeric(df, "close")

    vwap = _safe_numeric(df, "vwap")

    df["vwap_dev"] = (close - vwap) / (vwap + 1e-9)

    return df


# ============================================================
# RSI overheat
# ============================================================

def _rsi_overheat(df):

    rsi = _safe_numeric(df, "rsi")

    df["rsi_overheat"] = (rsi > 75).astype(int)

    return df


# ============================================================
# blow-off score
# ============================================================

def _blowoff_score(df):

    df["blowoff_score"] = (
        df["volume_climax"] * 30 +
        df["price_accel"] * 10 +
        df["vwap_dev"] * 40 +
        df["rsi_overheat"] * 20
    )

    return df


# ============================================================
# main detector
# ============================================================

def detect_blowoff_top(df: pd.DataFrame) -> pd.DataFrame:

    try:

        if df is None or df.empty:
            return pd.DataFrame()

        df = df.copy()

        df = _volume_climax(df)

        df = _price_accel(df)

        df = _vwap_dev(df)

        df = _rsi_overheat(df)

        df = _blowoff_score(df)

        cond = df["blowoff_score"] > 80

        result = df.loc[cond].copy()

        if len(result) > 0:

            logger.info(
                "[BLOWOFF] detected %s symbols",
                len(result)
            )

        return result

    except Exception:

        logger.exception("[blowoff_detector] failed")

        return pd.DataFrame()