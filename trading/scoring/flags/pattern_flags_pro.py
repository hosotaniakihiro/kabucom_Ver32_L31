# ============================================================
# File   : trading/scoring/flags/structure_flags_pro.py
# Version: PRO-STRUCTURE-FLAGS-V1
# ------------------------------------------------------------
# ✔ 市場構造FLAG生成
# ✔ breakout / breakdown
# ✔ volatility expansion
# ✔ VWAP structure
# ✔ squeeze detection
# ✔ spike detection
# ✔ volume clusters
# ✔ score_config.ini互換
# ✔ NaN / inf 完全防御
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

def generate_structure_flags(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    df = df.copy()

    close = _safe(_col(df, "close_price", "close"))
    high = _safe(_col(df, "high_price", "high"))
    low = _safe(_col(df, "low_price", "low"))
    volume = _safe(_col(df, "volume"))

    vwap = _safe(_col(df, "vwap"))

    bb_upper = _safe(_col(df, "bb_upper"))
    bb_lower = _safe(_col(df, "bb_lower"))

    atr = _safe(_col(df, "atr"))

    if close is None:
        return df

    # --------------------------------------------------------
    # breakout structure
    # --------------------------------------------------------

    high20 = high.rolling(20).max()
    low20 = low.rolling(20).min()

    df["flag_breakout_high"] = (
        close >= high20
    ).astype(int)

    df["flag_breakdown_3"] = (
        close <= low20
    ).astype(int)

    # --------------------------------------------------------
    # volatility expansion
    # --------------------------------------------------------

    if atr is not None:

        atr_avg = atr.rolling(20).mean()

        df["flag_volatility_high"] = (
            atr > atr_avg * 1.5
        ).astype(int)

        df["flag_volatility_expansion"] = (
            atr > atr.shift(1)
        ).astype(int)

    # --------------------------------------------------------
    # VWAP structure
    # --------------------------------------------------------

    if vwap is not None:

        df["flag_vwap_support"] = (
            (close > vwap)
            & (close.shift(1) <= vwap.shift(1))
        ).astype(int)

        df["flag_vwap_resistance"] = (
            (close < vwap)
            & (close.shift(1) >= vwap.shift(1))
        ).astype(int)

        df["flag_vwap_trend_up"] = (
            vwap > vwap.shift(5)
        ).astype(int)

        df["flag_vwap_trend_down"] = (
            vwap < vwap.shift(5)
        ).astype(int)

    # --------------------------------------------------------
    # Bollinger squeeze
    # --------------------------------------------------------

    if bb_upper is not None and bb_lower is not None:

        width = bb_upper - bb_lower
        width_avg = width.rolling(20).mean()

        df["flag_bb_squeeze"] = (
            width < width_avg * 0.5
        ).astype(int)

        df["flag_bb_expansion"] = (
            width > width_avg * 1.5
        ).astype(int)

    # --------------------------------------------------------
    # spike detection
    # --------------------------------------------------------

    if high is not None and low is not None:

        spread = high - low
        spread_avg = spread.rolling(20).mean()

        df["flag_price_spike"] = (
            spread > spread_avg * 2
        ).astype(int)

    # --------------------------------------------------------
    # volume cluster
    # --------------------------------------------------------

    if volume is not None:

        vol_avg = volume.rolling(50).mean()

        df["flag_volume_cluster"] = (
            volume > vol_avg * 2
        ).astype(int)

        df["flag_volume_exhaustion"] = (
            (volume < volume.shift(1))
            & (volume.shift(1) > vol_avg * 2)
        ).astype(int)

    # --------------------------------------------------------
    # momentum spike structure
    # --------------------------------------------------------

    if close is not None:

        momentum = close - close.shift(3)

        df["flag_momentum_spike"] = (
            momentum.abs() > momentum.abs().rolling(20).mean() * 2
        ).astype(int)

    return df