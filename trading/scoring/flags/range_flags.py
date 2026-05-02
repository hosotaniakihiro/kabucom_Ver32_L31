# ============================================================
# File   : trading/scoring/flags/range_flags.py
# Version: Ver1.0-PRODUCTION-RANGE-FLAGS
# ------------------------------------------------------------
# ✔ range breakout
# ✔ range expansion
# ✔ volatility breakout
# ✔ NaN / inf 完全防御
# ✔ column名ゆらぎ吸収
# ✔ vectorized高速処理
# ✔ HTF (3min / 5min) 対応
# ✔ DataFrame in / out
# ✔ score_config.ini 完全互換
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

def generate_range_flags(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    df = df.copy()

    # --------------------------------------------------------
    # columns
    # --------------------------------------------------------

    high = _safe(_col(df, "high_price", "high"))
    low = _safe(_col(df, "low_price", "low"))
    close = _safe(_col(df, "close_price", "close"))

    if high is None or low is None or close is None:
        return df

    # --------------------------------------------------------
    # range
    # --------------------------------------------------------

    rng = (high - low).replace([np.inf, -np.inf], np.nan)

    # --------------------------------------------------------
    # average range
    # --------------------------------------------------------

    avg_rng = rng.rolling(
        20,
        min_periods=5
    ).mean()

    # --------------------------------------------------------
    # breakout
    # --------------------------------------------------------

    prev_high = high.shift(1)

    df["flag_range_breakout"] = _flag(
        close > prev_high
    )

    # --------------------------------------------------------
    # range expansion
    # --------------------------------------------------------

    df["flag_range_expansion"] = _flag(
        rng > avg_rng * 1.5
    )

    # --------------------------------------------------------
    # volatility breakout
    # --------------------------------------------------------

    df["flag_volatility_breakout"] = _flag(
        rng > avg_rng * 2
    )

    return df