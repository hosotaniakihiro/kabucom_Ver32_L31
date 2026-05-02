# ============================================================
# File   : trading/scoring/flags/opening_range_flags.py
# Version: Ver1.0-PRODUCTION-OPENING-RANGE-FLAGS
# ------------------------------------------------------------
# ✔ flag_opening_range_break
# ✔ flag_opening_range_retest
# ✔ flag_opening_range_fail
# ✔ flag_opening_range_expansion
# ✔ intraday OR(5bar / 15bar proxy) 対応
# ✔ score_config.ini 互換
# ✔ vectorized高速処理
# ✔ DataFrame in / out
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


def _date_key(df: pd.DataFrame):
    dt = _col(df, "datetime", "timestamp", "inserted_at", "updated_at")
    if dt is None:
        return pd.Series(["ALL"] * len(df), index=df.index)
    try:
        dt2 = pd.to_datetime(dt, errors="coerce")
        return dt2.dt.strftime("%Y-%m-%d").fillna("ALL")
    except Exception:
        return pd.Series(["ALL"] * len(df), index=df.index)


def generate_opening_range_flags(
    df: pd.DataFrame,
    *,
    or_bars: int = 5,
) -> pd.DataFrame:
    if df is None or df.empty:
        return df

    df = df.copy()

    high = _safe(_col(df, "high_price", "high"))
    low = _safe(_col(df, "low_price", "low"))
    close = _safe(_col(df, "close_price", "close"))
    volume = _safe(_col(df, "volume"))

    if high is None or low is None or close is None:
        return df

    date_key = _date_key(df)

    try:
        bar_index = df.groupby(date_key).cumcount()
    except Exception:
        bar_index = pd.Series(range(len(df)), index=df.index)

    def _or_high(s):
        try:
            head = s.iloc[:or_bars]
            if len(head) == 0:
                return pd.Series(np.nan, index=s.index)
            return pd.Series([head.max()] * len(s), index=s.index)
        except Exception:
            return pd.Series(np.nan, index=s.index)

    def _or_low(s):
        try:
            head = s.iloc[:or_bars]
            if len(head) == 0:
                return pd.Series(np.nan, index=s.index)
            return pd.Series([head.min()] * len(s), index=s.index)
        except Exception:
            return pd.Series(np.nan, index=s.index)

    try:
        opening_range_high = high.groupby(date_key, group_keys=False).apply(_or_high)
        opening_range_low = low.groupby(date_key, group_keys=False).apply(_or_low)
    except Exception:
        opening_range_high = pd.Series(np.nan, index=df.index)
        opening_range_low = pd.Series(np.nan, index=df.index)

    df["opening_range_high"] = opening_range_high
    df["opening_range_low"] = opening_range_low
    df["opening_range_width"] = (opening_range_high - opening_range_low).replace([np.inf, -np.inf], np.nan)

    df["flag_opening_range_break"] = _flag(
        (bar_index >= or_bars) &
        (close > opening_range_high)
    )

    df["flag_opening_range_retest"] = _flag(
        (bar_index >= or_bars) &
        (low <= opening_range_high) &
        (close >= opening_range_high)
    )

    df["flag_opening_range_fail"] = _flag(
        (bar_index >= or_bars) &
        (close < opening_range_low)
    )

    avg_width = df["opening_range_width"].rolling(20, min_periods=5).mean()
    df["flag_opening_range_expansion"] = _flag(
        df["opening_range_width"] > avg_width * 1.2
    )

    if volume is not None:
        vol_avg = volume.rolling(20, min_periods=5).mean()
        df["flag_opening_range_break_volume"] = _flag(
            (df["flag_opening_range_break"] == 1) &
            (volume > vol_avg * 1.5)
        )

    return df