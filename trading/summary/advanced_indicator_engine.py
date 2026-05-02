# ==========================================================
# File   : trading/summary/advanced_indicator_engine.py
# Version: Ver1.0-INSTITUTIONAL-MOMENTUM-ENGINE
# ----------------------------------------------------------
# ✔ MA trend
# ✔ VWAP deviation
# ✔ RSI
# ✔ MACD
# ✔ volume spike
# ✔ breakout detector
# ✔ momentum score
# ✔ slope acceleration
# ✔ intraday acceleration
# ✔ 급騰株 detection
# ✔ vectorized
# ✔ production safe
# ==========================================================

from __future__ import annotations

import pandas as pd
import numpy as np


# ==========================================================
# moving averages
# ==========================================================

def _ma(df):

    df["ma5"] = df["close"].rolling(5).mean()
    df["ma25"] = df["close"].rolling(25).mean()
    df["ma75"] = df["close"].rolling(75).mean()

    return df


# ==========================================================
# RSI
# ==========================================================

def _rsi(df, period=14):

    delta = df["close"].diff()

    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()

    rs = avg_gain / (avg_loss + 1e-9)

    df["rsi"] = 100 - (100 / (1 + rs))

    return df


# ==========================================================
# MACD
# ==========================================================

def _macd(df):

    ema12 = df["close"].ewm(span=12).mean()
    ema26 = df["close"].ewm(span=26).mean()

    df["macd"] = ema12 - ema26
    df["macd_signal"] = df["macd"].ewm(span=9).mean()

    return df


# ==========================================================
# VWAP deviation
# ==========================================================

def _vwap(df):

    if "vwap" not in df.columns:
        return df

    df["vwap_dev"] = (df["close"] - df["vwap"]) / (df["vwap"] + 1e-9)

    return df


# ==========================================================
# volume spike
# ==========================================================

def _volume_spike(df):

    vol_ma = df["volume"].rolling(20).mean()

    df["volume_spike"] = df["volume"] / (vol_ma + 1e-9)

    return df


# ==========================================================
# breakout detector
# ==========================================================

def _breakout(df):

    high20 = df["high"].rolling(20).max()

    df["breakout"] = (df["close"] > high20.shift(1)).astype(int)

    return df


# ==========================================================
# momentum score
# ==========================================================

def _momentum(df):

    df["ret1"] = df["close"].pct_change()
    df["ret5"] = df["close"].pct_change(5)

    df["momentum"] = (
        df["ret1"] * 0.4 +
        df["ret5"] * 0.6
    )

    return df


# ==========================================================
# slope acceleration
# ==========================================================

def _slope(df):

    df["ma5_slope"] = df["ma5"].diff()
    df["ma25_slope"] = df["ma25"].diff()

    df["slope_accel"] = df["ma5_slope"].diff()

    return df


# ==========================================================
# intraday acceleration
# ==========================================================

def _intraday_accel(df):

    df["price_accel"] = df["close"].diff().diff()

    return df


# ==========================================================
# main engine
# ==========================================================

def apply_momentum_engine(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    df = df.copy()

    if "close" not in df.columns:
        return df

    df = _ma(df)
    df = _rsi(df)
    df = _macd(df)
    df = _vwap(df)
    df = _volume_spike(df)
    df = _breakout(df)
    df = _momentum(df)
    df = _slope(df)
    df = _intraday_accel(df)

    return df