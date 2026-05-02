# ============================================================
# File   : trading/scoring/flags/structure_flags_pro.py
# Version: Ver1.0-PRO-MARKET-STRUCTURE-FLAGS
# ------------------------------------------------------------
# ✔ Market Structure detection
# ✔ Higher High / Higher Low
# ✔ Lower High / Lower Low
# ✔ Break Structure
# ✔ Range Compression / Expansion
# ✔ Trend Phase detection
# ✔ Market Shift detection
# ✔ NaN / inf safe
# ✔ vectorized processing
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

def generate_structure_flags_pro(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    df = df.copy()

    high = _safe(_col(df, "high_price", "high"))
    low = _safe(_col(df, "low_price", "low"))
    close = _safe(_col(df, "close_price", "close"))

    if high is None or low is None:
        return df

    # --------------------------------------------------------
    # swing points
    # --------------------------------------------------------

    prev_high = high.shift(1)
    prev_low = low.shift(1)

    prev_high2 = high.shift(2)
    prev_low2 = low.shift(2)

    # ========================================================
    # Higher High
    # ========================================================

    df["flag_structure_higher_high"] = _flag(
        (high > prev_high) &
        (prev_high > prev_high2)
    )

    # ========================================================
    # Higher Low
    # ========================================================

    df["flag_structure_higher_low"] = _flag(
        (low > prev_low) &
        (prev_low > prev_low2)
    )

    # ========================================================
    # Lower High
    # ========================================================

    df["flag_structure_lower_high"] = _flag(
        (high < prev_high) &
        (prev_high < prev_high2)
    )

    # ========================================================
    # Lower Low
    # ========================================================

    df["flag_structure_lower_low"] = _flag(
        (low < prev_low) &
        (prev_low < prev_low2)
    )

    # ========================================================
    # Break Structure
    # ========================================================

    swing_high = high.shift(1).rolling(10).max()
    swing_low = low.shift(1).rolling(10).min()

    df["flag_structure_break_up"] = _flag(
        high > swing_high
    )

    df["flag_structure_break_down"] = _flag(
        low < swing_low
    )

    # ========================================================
    # Range compression
    # ========================================================

    range_size = (high - low)

    avg_range = range_size.rolling(20).mean()

    df["flag_structure_range_compression"] = _flag(
        range_size < avg_range * 0.6
    )

    # ========================================================
    # Range expansion
    # ========================================================

    df["flag_structure_range_expansion"] = _flag(
        range_size > avg_range * 1.5
    )

    # ========================================================
    # Trend phase
    # ========================================================

    if close is not None:

        ma = close.rolling(20).mean()

        df["flag_structure_trend_phase"] = _flag(
            close > ma
        )

    else:

        df["flag_structure_trend_phase"] = 0

    # ========================================================
    # Market shift
    # ========================================================

    df["flag_structure_market_shift"] = _flag(

        (df["flag_structure_lower_low"] == 1) &
        (df["flag_structure_break_down"] == 1)

    )

    return df