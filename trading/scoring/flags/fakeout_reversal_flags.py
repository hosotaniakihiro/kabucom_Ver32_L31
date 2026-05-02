# ============================================================
# File   : trading/scoring/flags/fakeout_reversal_flags.py
# Version: Ver1.0-PRODUCTION-FAKEOUT-REVERSAL-FLAGS
# ------------------------------------------------------------
# ✔ flag_fakeout_reclaim
# ✔ flag_ma_fakeout_reclaim
# ✔ flag_vwap_fakeout_reclaim
# ✔ flag_break_low_reclaim
# ✔ フェイク否定を検出
# ============================================================

from __future__ import annotations

import pandas as pd
import numpy as np


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


def _col(df, *names):
    lower_map = {c.lower(): c for c in df.columns}
    for n in names:
        if n in df.columns:
            return df[n]
        if n.lower() in lower_map:
            return df[lower_map[n.lower()]]
    return None


def _flag(expr):
    try:
        return expr.fillna(False).astype(int)
    except Exception:
        return pd.Series(0, index=expr.index)


def generate_fakeout_reversal_flags(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return df

    df = df.copy()

    close = _safe(_col(df, "close_price", "close"))
    low = _safe(_col(df, "low_price", "low"))
    ma5 = _safe(_col(df, "ma5"))
    vwap = _safe(_col(df, "vwap"))

    if close is None:
        return df

    if low is None:
        low = pd.Series(np.nan, index=df.index)

    prev_low = low.shift(1)

    ma_fake = pd.Series(False, index=df.index)
    if ma5 is not None:
        ma_fake = (close.shift(1) < ma5.shift(1)) & (close >= ma5)

    vwap_fake = pd.Series(False, index=df.index)
    if vwap is not None:
        vwap_fake = (close.shift(1) < vwap.shift(1)) & (close >= vwap)

    break_low_reclaim = (close.shift(1) < prev_low.shift(1)) & (close > prev_low)

    df["flag_ma_fakeout_reclaim"] = _flag(ma_fake)
    df["flag_vwap_fakeout_reclaim"] = _flag(vwap_fake)
    df["flag_break_low_reclaim"] = _flag(break_low_reclaim)
    df["flag_fakeout_reclaim"] = _flag(
        ma_fake | vwap_fake | break_low_reclaim
    )

    return df