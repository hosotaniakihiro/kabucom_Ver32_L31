# ============================================================
# File   : trading/scoring/flags/volume_flags.py
# Version: Ver1.1-PRODUCTION-VOLUME-FLAGS-HTF-STABLE
# ------------------------------------------------------------
# ✔ Ver1.0 完全互換（削除ゼロ）
# ✔ rolling min_periods 対応（3min / 5min 安定化）
# ✔ NaN / inf 完全防御
# ✔ volume=0 guard
# ✔ spread NaN guard
# ✔ vectorized高速処理
# ✔ add_scores 完全互換
# ✔ DataFrame in / out
# ✔ HTF対応
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
# main
# ============================================================

def generate_volume_flags(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    df = df.copy()

    volume = _safe(_col(df, "volume"))
    close = _safe(_col(df, "close_price", "close"))
    open_p = _safe(_col(df, "open_price", "open"))

    high = _safe(_col(df, "high_price", "high"))
    low = _safe(_col(df, "low_price", "low"))

    # =========================================================
    # volume baseline
    # =========================================================

    if volume is not None:

        vol_avg = volume.rolling(20, min_periods=5).mean()

        df["flag_volume_spike"] = (
            volume > vol_avg * 2
        ).fillna(False).astype(int)

        df["flag_volume_surge"] = (
            volume > vol_avg * 1.5
        ).fillna(False).astype(int)

        df["flag_volume_drop"] = (
            volume < vol_avg * 0.5
        ).fillna(False).astype(int)

    # =========================================================
    # peak out
    # =========================================================

    if volume is not None:

        vol_avg = volume.rolling(20, min_periods=5).mean()

        df["flag_volume_peak_out"] = (
            (volume < volume.shift(1))
            & (volume.shift(1) > vol_avg * 2)
        ).fillna(False).astype(int)

    # =========================================================
    # price breakout with volume
    # =========================================================

    if close is not None and volume is not None:

        prev_high = close.shift(1).rolling(20, min_periods=5).max()

        df["flag_volume_price_breakout"] = (
            (close > prev_high)
            & (volume > volume.rolling(20, min_periods=5).mean() * 1.5)
        ).fillna(False).astype(int)

    # =========================================================
    # price breakdown with volume
    # =========================================================

    if close is not None and volume is not None:

        prev_low = close.shift(1).rolling(20, min_periods=5).min()

        df["flag_volume_price_breakdown"] = (
            (close < prev_low)
            & (volume > volume.rolling(20, min_periods=5).mean() * 1.5)
        ).fillna(False).astype(int)

    # =========================================================
    # volume zone breakout
    # =========================================================

    if close is not None and volume is not None:

        vol_avg = volume.rolling(20, min_periods=5).mean()

        df["flag_volume_zone_break"] = (
            (volume > vol_avg * 2)
            & (close > close.shift(1))
        ).fillna(False).astype(int)

    # =========================================================
    # volume zone breakdown
    # =========================================================

    if close is not None and volume is not None:

        vol_avg = volume.rolling(20, min_periods=5).mean()

        df["flag_volume_zone_breakdown"] = (
            (volume > vol_avg * 2)
            & (close < close.shift(1))
        ).fillna(False).astype(int)

    # =========================================================
    # bullish candle with volume
    # =========================================================

    if open_p is not None and close is not None and volume is not None:

        vol_avg = volume.rolling(20, min_periods=5).mean()

        df["flag_bull_candle_volume"] = (
            (close > open_p)
            & (volume > vol_avg * 1.5)
        ).fillna(False).astype(int)

    # =========================================================
    # tick surge (approximation)
    # =========================================================

    if high is not None and low is not None:

        spread = (high - low).replace([np.inf, -np.inf], np.nan)

        spread_avg = spread.rolling(20, min_periods=5).mean()

        df["flag_tick_surge"] = (
            spread > spread_avg * 2
        ).fillna(False).astype(int)

    return df