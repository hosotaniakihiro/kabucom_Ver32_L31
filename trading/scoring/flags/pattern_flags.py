# ============================================================
# File   : trading/scoring/flags/pattern_flags.py
# Version: Ver1.0-FULL-CANDLE-PATTERN-FLAGS
# ------------------------------------------------------------
# ✔ score_config.ini ローソク足パターン flag 対応
# ✔ NaN / inf 完全防御
# ✔ indicator欠損安全
# ✔ vectorized高速処理
# ✔ add_scores 完全互換
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

def generate_pattern_flags(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    df = df.copy()

    o = _safe(_col(df, "open_price", "open"))
    c = _safe(_col(df, "close_price", "close"))
    h = _safe(_col(df, "high_price", "high"))
    l = _safe(_col(df, "low_price", "low"))

    if o is None or c is None or h is None or l is None:
        return df

    body = (c - o).abs()
    range_ = h - l
    upper = h - np.maximum(o, c)
    lower = np.minimum(o, c) - l

    # --------------------------------------------------------
    # bullish engulfing
    # --------------------------------------------------------

    df["flag_bullish_engulfing"] = (
        (c > o) &
        (c.shift(1) < o.shift(1)) &
        (c >= o.shift(1)) &
        (o <= c.shift(1))
    ).astype(int)

    # --------------------------------------------------------
    # bearish engulfing
    # --------------------------------------------------------

    df["flag_bearish_engulfing"] = (
        (c < o) &
        (c.shift(1) > o.shift(1)) &
        (c <= o.shift(1)) &
        (o >= c.shift(1))
    ).astype(int)

    # stronger engulfing
    df["flag_bearish_engulfing2"] = (
        (c < o) &
        (c.shift(1) > o.shift(1)) &
        (body > body.shift(1))
    ).astype(int)

    # --------------------------------------------------------
    # hammer
    # --------------------------------------------------------

    df["flag_hammer"] = (
        (lower > body * 2) &
        (upper < body)
    ).astype(int)

    # inverted hammer
    df["flag_inverted_hammer"] = (
        (upper > body * 2) &
        (lower < body)
    ).astype(int)

    # --------------------------------------------------------
    # dragonfly doji
    # --------------------------------------------------------

    df["flag_dragonfly_doji"] = (
        (body < range_ * 0.1) &
        (lower > range_ * 0.6)
    ).astype(int)

    # --------------------------------------------------------
    # shooting star
    # --------------------------------------------------------

    df["flag_shooting_star"] = (
        (upper > body * 2) &
        (lower < body)
    ).astype(int)

    # --------------------------------------------------------
    # hanging man
    # --------------------------------------------------------

    df["flag_hanging_man"] = (
        (lower > body * 2) &
        (c < o)
    ).astype(int)

    # --------------------------------------------------------
    # morning star
    # --------------------------------------------------------

    df["flag_morning_star"] = (
        (c.shift(2) < o.shift(2)) &
        (body.shift(1) < body.shift(2)) &
        (c > o)
    ).astype(int)

    # --------------------------------------------------------
    # evening star
    # --------------------------------------------------------

    df["flag_evening_star"] = (
        (c.shift(2) > o.shift(2)) &
        (body.shift(1) < body.shift(2)) &
        (c < o)
    ).astype(int)

    # --------------------------------------------------------
    # piercing line
    # --------------------------------------------------------

    df["flag_piercing_line"] = (
        (c.shift(1) < o.shift(1)) &
        (c > (o.shift(1) + c.shift(1)) / 2)
    ).astype(int)

    # --------------------------------------------------------
    # dark cloud cover
    # --------------------------------------------------------

    df["flag_dark_cloud_cover"] = (
        (c.shift(1) > o.shift(1)) &
        (c < (o.shift(1) + c.shift(1)) / 2)
    ).astype(int)

    # --------------------------------------------------------
    # bullish harami
    # --------------------------------------------------------

    df["flag_bullish_harami"] = (
        (c > o) &
        (body < body.shift(1))
    ).astype(int)

    # bearish harami
    df["flag_bearish_harami"] = (
        (c < o) &
        (body < body.shift(1))
    ).astype(int)

    # --------------------------------------------------------
    # three black crows
    # --------------------------------------------------------

    df["flag_three_black_crows"] = (
        (c < o) &
        (c.shift(1) < o.shift(1)) &
        (c.shift(2) < o.shift(2))
    ).astype(int)

    # --------------------------------------------------------
    # rising three methods
    # --------------------------------------------------------

    df["flag_rising_three_methods"] = (
        (c.shift(4) < c) &
        (body.shift(1) < body.shift(4))
    ).astype(int)

    # --------------------------------------------------------
    # window gap patterns
    # --------------------------------------------------------

    df["flag_window_up"] = (
        l > h.shift(1)
    ).astype(int)

    df["flag_window_down"] = (
        h < l.shift(1)
    ).astype(int)

    # --------------------------------------------------------
    # gapdown red
    # --------------------------------------------------------

    df["flag_gapdown_red"] = (
        (o < c.shift(1)) &
        (c < o)
    ).astype(int)

    return df