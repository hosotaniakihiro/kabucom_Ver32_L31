# ============================================================
# File   : trading/scoring/flags/momentum_flags_pro.py
# Version: PRO-MOMENTUM-FLAGS-V1
# ------------------------------------------------------------
# ✔ モメンタム系FLAG生成
# ✔ MACD / RSI / RCI / Bollinger
# ✔ score_config.ini 完全互換
# ✔ NaN / inf 完全防御
# ✔ 列名ゆらぎ吸収
# ✔ vectorized高速処理
# ✔ DataFrame in / out
# ============================================================

from __future__ import annotations

import pandas as pd
import numpy as np


# ============================================================
# safe numeric
# ============================================================

def _safe(series):

    if series is None:
        return None

    try:
        return (
            pd.to_numeric(series, errors="coerce")
            .replace([np.inf, -np.inf], np.nan)
        )
    except Exception:
        return series


# ============================================================
# column helper
# ============================================================

def _col(df, *names):

    lower_map = {c.lower(): c for c in df.columns}

    for n in names:

        if n in df.columns:
            return df[n]

        if n.lower() in lower_map:
            return df[lower_map[n.lower()]]

    return None


# ============================================================
# main
# ============================================================

def generate_momentum_flags(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    df = df.copy()

    macd = _safe(_col(df, "macd"))
    signal = _safe(_col(df, "signal"))
    hist = _safe(_col(df, "hist"))

    rsi = _safe(_col(df, "rsi"))
    rci = _safe(_col(df, "rci"))

    close = _safe(_col(df, "close_price", "close"))

    bb_upper = _safe(_col(df, "bb_upper"))
    bb_lower = _safe(_col(df, "bb_lower"))
    bb_mid = _safe(_col(df, "bb_mid"))

    # --------------------------------------------------------
    # MACD cross
    # --------------------------------------------------------

    if macd is not None and signal is not None:

        df["flag_macd_cross"] = (
            (macd > signal) &
            (macd.shift(1) <= signal.shift(1))
        ).astype(int)

        df["flag_macd_dc"] = (
            (macd < signal) &
            (macd.shift(1) >= signal.shift(1))
        ).astype(int)

    # --------------------------------------------------------
    # MACD histogram expansion
    # --------------------------------------------------------

    if hist is not None:

        df["flag_macd_hist_expand"] = (
            hist > hist.shift(1)
        ).astype(int)

    # --------------------------------------------------------
    # RSI rebound / fall
    # --------------------------------------------------------

    if rsi is not None:

        df["flag_rsi_rebound"] = (
            (rsi > 30) &
            (rsi.shift(1) <= 30)
        ).astype(int)

        df["flag_rsi_falling"] = (
            rsi < rsi.shift(1)
        ).astype(int)

        df["flag_rsi_oversold_30"] = (
            rsi <= 30
        ).astype(int)

        df["flag_rsi_overbought_70"] = (
            rsi >= 70
        ).astype(int)

    # --------------------------------------------------------
    # RCI momentum
    # --------------------------------------------------------

    if rci is not None:

        df["flag_rci_rising"] = (
            rci > rci.shift(1)
        ).astype(int)

        df["flag_rci9_uptrend"] = (
            rci > 0
        ).astype(int)

        df["flag_rci_trio_up"] = (
            (rci > 0) &
            (rci.shift(1) > 0) &
            (rci.shift(2) > 0)
        ).astype(int)

    # --------------------------------------------------------
    # Bollinger rebound
    # --------------------------------------------------------

    if bb_lower is not None and close is not None:

        df["flag_bb_lower_touch"] = (
            close <= bb_lower
        ).astype(int)

        df["flag_bollinger_rebound"] = (
            (close > bb_lower) &
            (close.shift(1) <= bb_lower.shift(1))
        ).astype(int)

    if bb_upper is not None and close is not None:

        df["flag_bb_upper_touch"] = (
            close >= bb_upper
        ).astype(int)

    # --------------------------------------------------------
    # Bollinger 3σ rebound / breakdown
    # --------------------------------------------------------

    if bb_upper is not None and bb_lower is not None and close is not None:

        width = bb_upper - bb_lower

        sigma3_lower = bb_lower - width
        sigma3_upper = bb_upper + width

        df["flag_bb_3sigma_rebound"] = (
            (close > sigma3_lower) &
            (close.shift(1) <= sigma3_lower.shift(1))
        ).astype(int)

        df["flag_bb_3sigma_breakdown"] = (
            (close < sigma3_upper) &
            (close.shift(1) >= sigma3_upper.shift(1))
        ).astype(int)

    # --------------------------------------------------------
    # Bollinger mean reversion
    # --------------------------------------------------------

    if bb_mid is not None and close is not None:

        df["flag_bollinger_rebound"] = (
            (close > bb_mid) &
            (close.shift(1) < bb_mid.shift(1))
        ).astype(int)

    return df