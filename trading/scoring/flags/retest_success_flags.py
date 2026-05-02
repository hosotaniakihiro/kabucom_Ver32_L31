# ============================================================
# File   : trading/scoring/flags/retest_success_flags.py
# Version: Ver1.0-PRODUCTION-RETEST-SUCCESS-FLAGS
# ------------------------------------------------------------
# ✔ flag_retest_success
# ✔ flag_breakout_level_retest
# ✔ flag_support_reclaim
# ✔ recent_breakout_level があれば使用
# ✔ 無ければ 20本高値近辺で近似
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


def generate_retest_success_flags(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return df

    df = df.copy()

    close = _safe(_col(df, "close_price", "close"))
    low = _safe(_col(df, "low_price", "low"))
    high = _safe(_col(df, "high_price", "high"))
    breakout_level = _safe(_col(df, "recent_breakout_level"))

    if close is None or low is None:
        return df

    if breakout_level is None:
        if high is not None:
            breakout_level = high.shift(1).rolling(20, min_periods=5).max()
        else:
            breakout_level = pd.Series(np.nan, index=df.index)

    tol = close * 0.003

    near_level = (low <= breakout_level + tol) & (low >= breakout_level - tol)
    reclaim = close >= breakout_level

    df["flag_breakout_level_retest"] = _flag(near_level)
    df["flag_support_reclaim"] = _flag(reclaim)
    df["flag_retest_success"] = _flag(near_level & reclaim)

    return df