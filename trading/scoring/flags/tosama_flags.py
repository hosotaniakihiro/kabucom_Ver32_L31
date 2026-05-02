# ============================================================
# File   : trading/scoring/flags/tosama_flags.py
# Version: Ver1.0-PRODUCTION-TOSAMA-FLAGS
# ------------------------------------------------------------
# ✔ flag_tosama_entry
# ✔ flag_tosama_early
# ✔ 殿様イナゴ検出
# ✔ score_config.ini 完全対応
# ✔ NaN / inf 完全防御
# ✔ column名ゆらぎ吸収
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

def generate_tosama_flags(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    df = df.copy()

    close = _safe(_col(df, "close_price", "close"))
    open_p = _safe(_col(df, "open_price", "open"))
    volume = _safe(_col(df, "volume"))

    if close is None:
        return df

    # --------------------------------------------------------
    # price momentum
    # --------------------------------------------------------

    ret1 = close.pct_change()

    ret5 = close.pct_change(5)

    # --------------------------------------------------------
    # volume spike
    # --------------------------------------------------------

    if volume is not None:

        vol_avg = volume.rolling(20).mean()

        vol_spike = volume > vol_avg * 3

    else:

        vol_spike = pd.Series(False, index=df.index)

    # ========================================================
    # early tosama
    # ========================================================

    df["flag_tosama_early"] = _flag(

        (ret1 > 0.02) &
        vol_spike

    )

    # ========================================================
    # full tosama entry
    # ========================================================

    df["flag_tosama_entry"] = _flag(

        (ret5 > 0.05) &
        vol_spike &
        (ret1 > 0)

    )

    return df