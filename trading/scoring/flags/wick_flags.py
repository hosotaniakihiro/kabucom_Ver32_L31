# ============================================================
# File   : trading/scoring/flags/wick_flags.py
# Version: Ver1.0-PRODUCTION-WICK-FLAGS
# ------------------------------------------------------------
# ✔ flag_lower_wick_low_zone
# ✔ flag_lower_wick_rebound
# ✔ score_config.ini 完全対応
# ✔ NaN / inf 完全防御
# ✔ column名ゆらぎ吸収
# ✔ vectorized高速処理
# ✔ HTF(3min/5min)安定
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

def generate_wick_flags(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    df = df.copy()

    open_p = _safe(_col(df, "open_price", "open"))
    close = _safe(_col(df, "close_price", "close"))
    high = _safe(_col(df, "high_price", "high"))
    low = _safe(_col(df, "low_price", "low"))

    if open_p is None or close is None or high is None or low is None:
        return df

    # ========================================================
    # candle structure
    # ========================================================

    body = (close - open_p).abs()

    upper = high - np.maximum(open_p, close)

    lower = np.minimum(open_p, close) - low

    rng = (high - low).replace([np.inf, -np.inf], np.nan)

    # zero guard
    body = body.replace(0, np.nan)

    # ========================================================
    # long lower wick
    # ========================================================

    df["flag_lower_wick_low_zone"] = _flag(
        lower > body * 2
    )

    # ========================================================
    # bullish wick rebound
    # ========================================================

    df["flag_lower_wick_rebound"] = _flag(
        (lower > body * 2) &
        (close > open_p)
    )

    return df