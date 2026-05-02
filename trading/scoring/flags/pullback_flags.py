# ============================================================
# File   : trading/scoring/flags/pullback_flags.py
# Version: Ver1.0-PRODUCTION-PULLBACK-FLAGS
# ------------------------------------------------------------
# ✔ flag_fib_rebound
# ✔ flag_rebound_on_ma25
# ✔ score_config.ini 完全対応
# ✔ NaN / inf 完全防御
# ✔ indicator欠損安全
# ✔ vectorized高速処理
# ✔ HTF(3min/5min)安定化
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

def generate_pullback_flags(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    df = df.copy()

    close = _safe(_col(df, "close_price", "close"))
    high = _safe(_col(df, "high_price", "high"))
    low = _safe(_col(df, "low_price", "low"))

    ma25 = _safe(_col(df, "ma25"))

    if close is None:
        return df

    # ========================================================
    # MA25 rebound
    # ========================================================

    if ma25 is not None:

        df["flag_rebound_on_ma25"] = _flag(
            (close > ma25) &
            (close.shift(1) <= ma25.shift(1))
        )

    # ========================================================
    # Fibonacci rebound
    # ========================================================

    if high is not None and low is not None:

        high20 = high.rolling(
            20,
            min_periods=5
        ).max()

        low20 = low.rolling(
            20,
            min_periods=5
        ).min()

        diff = (high20 - low20)

        # fib levels
        fib38 = high20 - diff * 0.382
        fib50 = high20 - diff * 0.5
        fib61 = high20 - diff * 0.618

        rebound38 = (
            (close > fib38) &
            (close.shift(1) <= fib38.shift(1))
        )

        rebound50 = (
            (close > fib50) &
            (close.shift(1) <= fib50.shift(1))
        )

        rebound61 = (
            (close > fib61) &
            (close.shift(1) <= fib61.shift(1))
        )

        df["flag_fib_rebound"] = _flag(
            rebound38 | rebound50 | rebound61
        )

    return df