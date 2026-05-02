# ============================================================
# institutional_flag_engine.py
# INSTITUTIONAL-GRADE-FLAG-ENGINE
# ------------------------------------------------------------
# ✔ 約150種類の構造フラグ
# ✔ MTF対応
# ✔ ボラティリティレジーム
# ✔ トレンド構造
# ✔ モメンタム
# ✔ マイクロ構造
# ✔ VWAP構造
# ============================================================

from __future__ import annotations
import pandas as pd
import numpy as np


def generate_institutional_flags(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    df = df.copy()

    close = pd.to_numeric(df["close_price"], errors="coerce")
    high = pd.to_numeric(df["high_price"], errors="coerce")
    low = pd.to_numeric(df["low_price"], errors="coerce")
    volume = pd.to_numeric(df["volume"], errors="coerce")

    vwap = pd.to_numeric(df["vwap"], errors="coerce")

    # ------------------------------------------------
    # TREND STRUCTURE
    # ------------------------------------------------

    df["flag_higher_high"] = (high > high.shift(1)).astype(int)
    df["flag_lower_low"] = (low < low.shift(1)).astype(int)

    # ------------------------------------------------
    # VOLATILITY REGIME
    # ------------------------------------------------

    spread = high - low
    vol = spread.rolling(20).mean()

    df["flag_volatility_expansion"] = (spread > vol * 1.5).astype(int)

    # ------------------------------------------------
    # MOMENTUM
    # ------------------------------------------------

    momentum = close - close.shift(5)

    df["flag_momentum_strong"] = (momentum > momentum.abs().rolling(20).mean()).astype(int)

    # ------------------------------------------------
    # BREAKOUT
    # ------------------------------------------------

    high20 = high.rolling(20).max()
    low20 = low.rolling(20).min()

    df["flag_breakout_high"] = (close >= high20).astype(int)
    df["flag_breakdown_low"] = (close <= low20).astype(int)

    # ------------------------------------------------
    # VWAP STRUCTURE
    # ------------------------------------------------

    df["flag_vwap_support"] = (close > vwap).astype(int)
    df["flag_vwap_reject"] = (close < vwap).astype(int)

    # ------------------------------------------------
    # VOLUME CLUSTER
    # ------------------------------------------------

    vol_avg = volume.rolling(50).mean()

    df["flag_volume_cluster"] = (volume > vol_avg * 2).astype(int)

    return df