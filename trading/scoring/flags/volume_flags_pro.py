# ============================================================
# File   : trading/scoring/flags/volume_flags_pro.py
# Version: PRO-VOLUME-FLAGS-V1
# ------------------------------------------------------------
# ✔ 出来高・価格連動FLAG生成
# ✔ volume spike / surge / drop / peak
# ✔ volume breakout / breakdown
# ✔ volume zone break / breakdown
# ✔ tick surge / bull candle volume
# ✔ score_config.ini 完全互換
# ✔ NaN / inf 防御
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

def generate_volume_flags(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    df = df.copy()

    volume = _safe(_col(df, "volume"))
    close = _safe(_col(df, "close_price", "close"))
    open_p = _safe(_col(df, "open_price", "open"))
    high = _safe(_col(df, "high_price", "high"))
    low = _safe(_col(df, "low_price", "low"))

    if volume is None:
        return df

    # --------------------------------------------------------
    # volume baseline
    # --------------------------------------------------------

    vol_avg20 = volume.rolling(20).mean()
    vol_avg50 = volume.rolling(50).mean()

    # --------------------------------------------------------
    # spike / surge
    # --------------------------------------------------------

    df["flag_volume_spike"] = (
        volume > vol_avg20 * 2
    ).astype(int)

    df["flag_volume_surge"] = (
        volume > vol_avg20 * 1.5
    ).astype(int)

    # --------------------------------------------------------
    # drop
    # --------------------------------------------------------

    df["flag_volume_drop"] = (
        volume < vol_avg20 * 0.5
    ).astype(int)

    # --------------------------------------------------------
    # peak out
    # --------------------------------------------------------

    df["flag_volume_peak_out"] = (
        (volume < volume.shift(1))
        & (volume.shift(1) > vol_avg20 * 2)
    ).astype(int)

    # --------------------------------------------------------
    # price breakout with volume
    # --------------------------------------------------------

    if close is not None:

        prev_high = close.shift(1).rolling(20).max()

        df["flag_volume_price_breakout"] = (
            (close > prev_high)
            & (volume > vol_avg20 * 1.5)
        ).astype(int)

    # --------------------------------------------------------
    # price breakdown with volume
    # --------------------------------------------------------

    if close is not None:

        prev_low = close.shift(1).rolling(20).min()

        df["flag_volume_price_breakdown"] = (
            (close < prev_low)
            & (volume > vol_avg20 * 1.5)
        ).astype(int)

    # --------------------------------------------------------
    # volume zone breakout
    # --------------------------------------------------------

    if close is not None:

        df["flag_volume_zone_break"] = (
            (volume > vol_avg20 * 2)
            & (close > close.shift(1))
        ).astype(int)

    # --------------------------------------------------------
    # volume zone breakdown
    # --------------------------------------------------------

    if close is not None:

        df["flag_volume_zone_breakdown"] = (
            (volume > vol_avg20 * 2)
            & (close < close.shift(1))
        ).astype(int)

    # --------------------------------------------------------
    # bullish candle with volume
    # --------------------------------------------------------

    if open_p is not None and close is not None:

        df["flag_bull_candle_volume"] = (
            (close > open_p)
            & (volume > vol_avg20 * 1.5)
        ).astype(int)

    # --------------------------------------------------------
    # tick surge (spread expansion)
    # --------------------------------------------------------

    if high is not None and low is not None:

        spread = high - low
        spread_avg = spread.rolling(20).mean()

        df["flag_tick_surge"] = (
            spread > spread_avg * 2
        ).astype(int)

    # --------------------------------------------------------
    # volume cluster (プロ版)
    # --------------------------------------------------------

    df["flag_volume_cluster"] = (
        volume > vol_avg50 * 2
    ).astype(int)

    # --------------------------------------------------------
    # volume acceleration
    # --------------------------------------------------------

    df["flag_volume_acceleration"] = (
        volume > volume.shift(1)
    ).astype(int)

    return df