# ============================================================
# File   : trading/scoring/flags/phase_shift_flags.py
# Version: Ver1.0-PRODUCTION-PHASE-SHIFT-FLAGS
# ------------------------------------------------------------
# ✔ flag_phase_shift
# ✔ flag_buy_over_sell_cross
# ✔ flag_phase_recovery
# ✔ score_buy / score_sell / vwap_reclaim / macd_cross 対応
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


def generate_phase_shift_flags(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return df

    df = df.copy()

    score_buy = _safe(_col(df, "score_buy"))
    score_sell = _safe(_col(df, "score_sell"))
    vwap_reclaim = _safe(_col(df, "flag_vwap_reclaim", "flag_vwap_breakout"))
    macd_cross = _safe(_col(df, "flag_macd_cross"))
    rsi_rebound = _safe(_col(df, "flag_rsi_rebound"))

    if score_buy is None or score_sell is None:
        return df

    if vwap_reclaim is None:
        vwap_reclaim = pd.Series(0, index=df.index)
    if macd_cross is None:
        macd_cross = pd.Series(0, index=df.index)
    if rsi_rebound is None:
        rsi_rebound = pd.Series(0, index=df.index)

    buy_gt_sell = score_buy > score_sell
    prev_buy_le_sell = score_buy.shift(1) <= score_sell.shift(1)

    df["flag_buy_over_sell_cross"] = _flag(
        buy_gt_sell & prev_buy_le_sell
    )

    df["flag_phase_shift"] = _flag(
        (df["flag_buy_over_sell_cross"] == 1) &
        ((vwap_reclaim > 0) | (macd_cross > 0) | (rsi_rebound > 0))
    )

    df["flag_phase_recovery"] = _flag(
        buy_gt_sell & ((macd_cross > 0) | (rsi_rebound > 0))
    )

    return df