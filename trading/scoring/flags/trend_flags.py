# ============================================================
# File   : trading/scoring/flags/trend_flags.py
# Version: Ver1.0-FULL-TREND-FLAGS
# ------------------------------------------------------------
# ✔ score_config.ini トレンド系 flag 対応
# ✔ NaN / inf 安全
# ✔ indicator欠損安全
# ✔ vectorized高速処理
# ✔ add_scores 完全互換
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
# helper
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

def generate_trend_flags(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    df = df.copy()

    close = _safe(_col(df, "close_price", "close"))
    ma5 = _safe(_col(df, "ma5"))
    ma25 = _safe(_col(df, "ma25"))
    ma75 = _safe(_col(df, "ma75"))
    vwap = _safe(_col(df, "vwap"))

    # --------------------------------------------------------
    # direction
    # --------------------------------------------------------

    if close is not None:

        df["flag_dir_up"] = (close > close.shift(1)).astype(int)

        df["flag_dir_down"] = (close < close.shift(1)).astype(int)

    # --------------------------------------------------------
    # MA cross
    # --------------------------------------------------------

    if ma5 is not None and ma25 is not None:

        df["flag_ma5_ma25_cross"] = (
            (ma5 > ma25)
            & (ma5.shift(1) <= ma25.shift(1))
        ).astype(int)

        df["flag_ma5_below_ma25"] = (ma5 < ma25).astype(int)

    # --------------------------------------------------------
    # MA alignment
    # --------------------------------------------------------

    if ma5 is not None and ma25 is not None and ma75 is not None:

        df["flag_ma_up"] = (
            (ma5 > ma25)
            & (ma25 > ma75)
        ).astype(int)

        df["flag_ma_alignment_down"] = (
            (ma5 < ma25)
            & (ma25 < ma75)
        ).astype(int)

    # --------------------------------------------------------
    # MA slope
    # --------------------------------------------------------

    if ma5 is not None:

        df["flag_ma5_downtrend"] = (ma5 < ma5.shift(1)).astype(int)

    # --------------------------------------------------------
    # perfect order
    # --------------------------------------------------------

    if ma5 is not None and ma25 is not None and ma75 is not None:

        df["flag_perfect_order_event"] = (
            (ma5 > ma25)
            & (ma25 > ma75)
        ).astype(int)

        df["flag_perfect_order_down"] = (
            (ma5 < ma25)
            & (ma25 < ma75)
        ).astype(int)

    # --------------------------------------------------------
    # price vs MA
    # --------------------------------------------------------

    if close is not None and ma75 is not None:

        df["flag_below_ma75"] = (close < ma75).astype(int)

    # --------------------------------------------------------
    # breakout high
    # --------------------------------------------------------

    if close is not None:

        rolling_high = close.rolling(20).max()

        df["flag_breakout_high"] = (
            close >= rolling_high
        ).astype(int)

    # --------------------------------------------------------
    # VWAP
    # --------------------------------------------------------

    if close is not None and vwap is not None:

        df["flag_vwap_break"] = (close > vwap).astype(int)

        df["flag_vwap_fail"] = (close < vwap).astype(int)

        df["flag_vwap_breakout"] = (
            (close > vwap)
            & (close.shift(1) <= vwap.shift(1))
        ).astype(int)

    # --------------------------------------------------------
    # first pullback
    # --------------------------------------------------------

    if close is not None and ma25 is not None:

        df["flag_first_pullback"] = (
            (close > ma25)
            & (close.shift(1) < ma25.shift(1))
        ).astype(int)

    # --------------------------------------------------------
    # gap breakout
    # --------------------------------------------------------

    open_p = _safe(_col(df, "open_price", "open"))

    if open_p is not None and close is not None:

        prev_high = close.shift(1).rolling(5).max()

        df["flag_gap_up_breakout"] = (
            open_p > prev_high
        ).astype(int)

    # --------------------------------------------------------
    # window up / down
    # --------------------------------------------------------

    high = _safe(_col(df, "high_price", "high"))
    low = _safe(_col(df, "low_price", "low"))

    if high is not None and low is not None:

        df["flag_window_up"] = (
            low > high.shift(1)
        ).astype(int)

        df["flag_window_down"] = (
            high < low.shift(1)
        ).astype(int)

    return df