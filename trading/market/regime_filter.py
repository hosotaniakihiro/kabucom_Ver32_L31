# ============================================================
# regime_filter.py
# Ver1.0-PRODUCTION-MARKET-REGIME
# ------------------------------------------------------------
# ✔ Market regime detection
# ✔ ATR volatility detection
# ✔ VWAP deviation
# ✔ Volume expansion
# ✔ Trend detection
# ✔ Entry allow / deny
# ============================================================

from __future__ import annotations
import logging
import numpy as np
import pandas as pd

from global_state import global_data

logger = logging.getLogger(__name__)

ATR_VOL_THRESHOLD = 0.015
VWAP_DEV_THRESHOLD = 0.01
VOLUME_EXPANSION = 1.5


# ============================================================
# Trend
# ============================================================

def detect_trend(df: pd.DataFrame):

    if "ma25" not in df.columns or "ma75" not in df.columns:
        return False

    slope = df["ma25"] - df["ma75"]

    return slope.mean() > 0


# ============================================================
# Volatility
# ============================================================

def detect_volatility(df: pd.DataFrame):

    if "atr" not in df.columns:
        return False

    atr_ratio = df["atr"] / df["close"]

    return atr_ratio.mean() > ATR_VOL_THRESHOLD


# ============================================================
# VWAP deviation
# ============================================================

def detect_vwap_expansion(df: pd.DataFrame):

    if "vwap" not in df.columns:
        return False

    dev = abs(df["close"] - df["vwap"]) / df["vwap"]

    return dev.mean() > VWAP_DEV_THRESHOLD


# ============================================================
# Volume expansion
# ============================================================

def detect_volume_expansion(df: pd.DataFrame):

    if "volume" not in df.columns:
        return False

    vol = df["volume"]

    if len(vol) < 10:
        return False

    recent = vol.tail(3).mean()
    base = vol.head(10).mean()

    if base == 0:
        return False

    return recent / base > VOLUME_EXPANSION


# ============================================================
# Main
# ============================================================

def detect_market_regime():

    summary = getattr(global_data, "summary_cache", {}).get("1min")

    if summary is None or summary.empty:
        return "UNKNOWN"

    try:

        trend = detect_trend(summary)
        vol = detect_volatility(summary)
        vwap = detect_vwap_expansion(summary)
        volume = detect_volume_expansion(summary)

        if vol and volume:
            regime = "VOLATILE"

        elif trend:
            regime = "TREND"

        elif not vol and not volume:
            regime = "DEAD"

        else:
            regime = "RANGE"

        logger.info("[REGIME] %s", regime)

        global_data.market_regime = regime

        return regime

    except Exception:

        logger.exception("regime detection failed")

        return "UNKNOWN"


# ============================================================
# Entry guard
# ============================================================

def allow_entry():

    regime = getattr(global_data, "market_regime", "UNKNOWN")

    if regime in ("TREND", "VOLATILE"):
        return True

    return False