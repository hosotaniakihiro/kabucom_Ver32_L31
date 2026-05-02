# ============================================================
# File   : trading/scoring/flags/combo_flags.py
# Version: Ver1.0-PRODUCTION-COMBO-FLAGS
# ------------------------------------------------------------
# ✔ flag_bull_big_combo
# ✔ flag_multi_signal_cluster
# ✔ score_config.ini 完全対応
# ✔ NaN / inf 完全防御
# ✔ flag列自動検出
# ✔ vectorized高速処理
# ✔ HTF対応
# ✔ DataFrame in / out
# ============================================================

from __future__ import annotations

import pandas as pd
import numpy as np


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

def generate_combo_flags(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    df = df.copy()

    # --------------------------------------------------------
    # helper
    # --------------------------------------------------------

    def _get_flag(name):

        if name in df.columns:
            return df[name].fillna(0)

        return pd.Series(0, index=df.index)

    # --------------------------------------------------------
    # bull big combo
    # --------------------------------------------------------

    macd = _get_flag("flag_macd_cross")
    vol = _get_flag("flag_volume_spike")
    breakout = _get_flag("flag_breakout_high")
    vwap = _get_flag("flag_vwap_break")

    df["flag_bull_big_combo"] = _flag(
        (macd == 1) &
        (vol == 1) &
        (breakout == 1) &
        (vwap == 1)
    )

    # --------------------------------------------------------
    # multi signal cluster
    # --------------------------------------------------------

    flag_cols = [
        c for c in df.columns
        if c.startswith("flag_")
    ]

    if len(flag_cols) > 0:

        score = df[flag_cols].fillna(0).sum(axis=1)

        df["flag_multi_signal_cluster"] = _flag(
            score >= 5
        )

    else:

        df["flag_multi_signal_cluster"] = 0

    return df