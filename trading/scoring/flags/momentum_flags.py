# ============================================================
# File   : trading/scoring/flags/momentum_flags.py
# Version: Ver1.1-PRODUCTION-MOMENTUM-FLAGS-ULTRA-STABLE
# ------------------------------------------------------------
# ✔ Ver1.0 機能完全保持（削除ゼロ）
# ✔ score_config.ini モメンタム系 flag 対応
# ✔ NaN / inf 完全防御
# ✔ indicator欠損安全
# ✔ vectorized高速処理
# ✔ add_scores 完全互換
# ✔ DataFrame in / out
# ✔ shift NaN 安全化
# ✔ Bollinger width guard
# ✔ dtype stabilization
# ✔ production safe
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

        s = pd.to_numeric(series, errors="coerce")

        if isinstance(s, pd.Series):
            s = s.replace([np.inf, -np.inf], np.nan)

        return s

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
# bool → int safe
# ============================================================

def _flag(expr):

    try:
        return expr.fillna(False).astype(int)
    except Exception:
        return pd.Series(0, index=expr.index)


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

    bb_upper = _safe(_col(df, "bb_upper"))
    bb_lower = _safe(_col(df, "bb_lower"))

    close = _safe(_col(df, "close_price", "close"))

    # --------------------------------------------------------
    # MACD cross
    # --------------------------------------------------------

    if macd is not None and signal is not None:

        df["flag_macd_cross"] = _flag(
            (macd > signal) &
            (macd.shift(1) <= signal.shift(1))
        )

        df["flag_macd_dc"] = _flag(
            (macd < signal) &
            (macd.shift(1) >= signal.shift(1))
        )

    # --------------------------------------------------------
    # MACD histogram expansion
    # --------------------------------------------------------

    if hist is not None:

        df["flag_macd_hist_expand"] = _flag(
            hist > hist.shift(1)
        )

    # --------------------------------------------------------
    # RSI rebound
    # --------------------------------------------------------

    if rsi is not None:

        df["flag_rsi_rebound"] = _flag(
            (rsi > 30) &
            (rsi.shift(1) <= 30)
        )

        df["flag_rsi_falling"] = _flag(
            rsi < rsi.shift(1)
        )

        df["flag_rsi_oversold_30"] = _flag(
            rsi <= 30
        )

        df["flag_rsi_overbought_70"] = _flag(
            rsi >= 70
        )

    # --------------------------------------------------------
    # RCI
    # --------------------------------------------------------

    if rci is not None:

        df["flag_rci_rising"] = _flag(
            rci > rci.shift(1)
        )

        df["flag_rci9_uptrend"] = _flag(
            rci > 0
        )

        df["flag_rci_trio_up"] = _flag(
            (rci > 0) &
            (rci.shift(1) > 0) &
            (rci.shift(2) > 0)
        )

    # --------------------------------------------------------
    # Bollinger rebound
    # --------------------------------------------------------

    if bb_lower is not None and close is not None:

        df["flag_bb_lower_touch"] = _flag(
            close <= bb_lower
        )

        df["flag_bollinger_rebound"] = _flag(
            (close > bb_lower) &
            (close.shift(1) <= bb_lower.shift(1))
        )

    if bb_upper is not None and close is not None:

        df["flag_bb_upper_touch"] = _flag(
            close >= bb_upper
        )

    # --------------------------------------------------------
    # BB 3σ rebound
    # --------------------------------------------------------

    if bb_upper is not None and bb_lower is not None and close is not None:

        width = (bb_upper - bb_lower)

        width = width.replace([np.inf, -np.inf], np.nan)

        sigma3_lower = bb_lower - width
        sigma3_upper = bb_upper + width

        df["flag_bb_3sigma_rebound"] = _flag(
            (close > sigma3_lower) &
            (close.shift(1) <= sigma3_lower.shift(1))
        )

        df["flag_bb_3sigma_breakdown"] = _flag(
            (close < sigma3_upper) &
            (close.shift(1) >= sigma3_upper.shift(1))
        )

    return df