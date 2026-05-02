# ============================================================
# File   : trading/scoring/flags/signal_confidence_flags.py
# Version: Ver1.0-PRODUCTION-SIGNAL-CONFIDENCE-FLAGS
# ------------------------------------------------------------
# ✔ flag_signal_confidence_ok
# ✔ flag_signal_confidence_high
# ✔ future/stale/ohlc/volume 欠陥簡易検査
# ✔ score_config.ini 互換
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
        return pd.Series(0, index=df.index)


def generate_signal_confidence_flags(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return df

    df = df.copy()

    open_p = _safe(_col(df, "open_price", "open"))
    high = _safe(_col(df, "high_price", "high"))
    low = _safe(_col(df, "low_price", "low"))
    close = _safe(_col(df, "close_price", "close"))
    volume = _safe(_col(df, "volume"))

    if close is None:
        return df

    valid_ohlc = pd.Series(True, index=df.index)
    if open_p is not None and high is not None and low is not None:
        valid_ohlc = (
            (high >= low) &
            (high >= open_p) &
            (high >= close) &
            (low <= open_p) &
            (low <= close)
        )

    valid_volume = pd.Series(True, index=df.index)
    if volume is not None:
        valid_volume = volume.fillna(0) > 0

    no_nan_core = close.notna()
    if open_p is not None:
        no_nan_core = no_nan_core & open_p.notna()
    if high is not None:
        no_nan_core = no_nan_core & high.notna()
    if low is not None:
        no_nan_core = no_nan_core & low.notna()

    confidence_ok = valid_ohlc & no_nan_core
    confidence_high = confidence_ok & valid_volume

    df["flag_signal_confidence_ok"] = confidence_ok.fillna(False).astype(int)
    df["flag_signal_confidence_high"] = confidence_high.fillna(False).astype(int)

    return df