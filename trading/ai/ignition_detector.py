# ============================================================
# File   : trading/ai/ignition_detector.py
# Version: Ver1.1-PRODUCTION-IGNITION-DETECTOR-DISCORD
# ------------------------------------------------------------
# ✔ 急騰株 ignition detection
# ✔ volume ignition
# ✔ VWAP breakout
# ✔ price acceleration
# ✔ ranking velocity
# ✔ liquidity filter
# ✔ Discord alert integration (NEW)
# ✔ NaN / inf safe
# ✔ vectorized
# ✔ production safe
# ============================================================

from __future__ import annotations

import logging
import pandas as pd
import numpy as np

from alerts import notify_ignition

logger = logging.getLogger(__name__)


# ============================================================
# safe numeric
# ============================================================

def _safe(df, col, default=0):

    try:

        if col not in df.columns:
            return pd.Series(default, index=df.index)

        s = pd.to_numeric(df[col], errors="coerce")

        s = s.replace([np.inf, -np.inf], np.nan)

        return s.fillna(default)

    except Exception:

        logger.exception("[ignition_detector] safe failed")

        return pd.Series(default, index=df.index)


# ============================================================
# volume ignition
# ============================================================

def _volume_ignition(df):

    volume = _safe(df, "volume")

    vol_ma = volume.rolling(20).mean()

    df["ignition_volume"] = volume / (vol_ma + 1e-9)

    return df


# ============================================================
# VWAP breakout
# ============================================================

def _vwap_breakout(df):

    close = _safe(df, "close")
    vwap = _safe(df, "vwap")

    df["ignition_vwap"] = (close > vwap).astype(int)

    return df


# ============================================================
# price acceleration
# ============================================================

def _price_accel(df):

    close = _safe(df, "close")

    df["ignition_price_accel"] = close.diff()

    return df


# ============================================================
# ranking velocity
# ============================================================

def _ranking_velocity(df):

    try:

        if "velocity_score" not in df.columns:

            df["ignition_velocity"] = 0

            return df

        df["ignition_velocity"] = df["velocity_score"]

        return df

    except Exception:

        logger.exception("[ignition_detector] velocity failed")

        df["ignition_velocity"] = 0

        return df


# ============================================================
# ignition score
# ============================================================

def _ignition_score(df):

    try:

        df["ignition_score"] = (
            df["ignition_volume"] * 40
            + df["ignition_vwap"] * 20
            + df["ignition_price_accel"] * 10
            + df["ignition_velocity"] * 15
        )

        return df

    except Exception:

        logger.exception("[ignition_detector] score failed")

        df["ignition_score"] = 0

        return df


# ============================================================
# main
# ============================================================

def apply_ignition_detector(df: pd.DataFrame) -> pd.DataFrame:

    try:

        if df is None or df.empty:
            return df

        df = df.copy()

        df = _volume_ignition(df)

        df = _vwap_breakout(df)

        df = _price_accel(df)

        df = _ranking_velocity(df)

        df = _ignition_score(df)

        return df

    except Exception:

        logger.exception("[ignition_detector] failed")

        return df


# ============================================================
# candidate detection
# ============================================================

def detect_ignition_candidates(df: pd.DataFrame) -> pd.DataFrame:

    try:

        if df is None or df.empty:
            return pd.DataFrame()

        df = apply_ignition_detector(df)

        cond = (
            (df["ignition_volume"] > 2)
            & (df["ignition_vwap"] == 1)
            & (df["ignition_score"] > 50)
        )

        result = df.loc[cond].copy()

        if len(result) > 0:

            logger.info(
                "[IGNITION] detected %s symbols",
                len(result)
            )

            # ----------------------------------------
            # Discord通知
            # ----------------------------------------

            try:

                notify_ignition(result)

            except Exception:

                logger.exception(
                    "[ignition_alert] failed"
                )

        return result

    except Exception:

        logger.exception("[ignition_detector] detection failed")

        return pd.DataFrame()