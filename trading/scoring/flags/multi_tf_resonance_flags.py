# ============================================================
# File   : trading/scoring/flags/multi_tf_resonance_flags.py
# Version: Ver1.0-PRODUCTION-MULTI-TF-RESONANCE-FLAGS
# ------------------------------------------------------------
# ✔ flag_multi_tf_resonance
# ✔ flag_tf3_ok
# ✔ flag_tf5_ok
# ✔ flag_mtf_consensus_up
# ✔ tf3_score / tf5_score / score_mtf 対応
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


def generate_multi_tf_resonance_flags(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return df

    df = df.copy()

    tf3 = _safe(_col(df, "tf3_score", "score_3m", "score3"))
    tf5 = _safe(_col(df, "tf5_score", "score_5m", "score5"))
    mtf = _safe(_col(df, "score_mtf", "mtf_score", "mtf"))
    macd_cross = _safe(_col(df, "flag_macd_cross"))
    rsi_rebound = _safe(_col(df, "flag_rsi_rebound"))
    breakout = _safe(_col(df, "flag_breakout_high"))

    if tf3 is None:
        tf3 = mtf
    if tf5 is None:
        tf5 = mtf
    if mtf is None:
        mtf = pd.Series(0.0, index=df.index)

    if tf3 is None and tf5 is None and mtf is None:
        return df

    if tf3 is None:
        tf3 = pd.Series(0.0, index=df.index)
    if tf5 is None:
        tf5 = pd.Series(0.0, index=df.index)

    if macd_cross is None:
        macd_cross = pd.Series(0, index=df.index)
    if rsi_rebound is None:
        rsi_rebound = pd.Series(0, index=df.index)
    if breakout is None:
        breakout = pd.Series(0, index=df.index)

    df["flag_tf3_ok"] = _flag(tf3 > 0)
    df["flag_tf5_ok"] = _flag(tf5 > 0)
    df["flag_mtf_consensus_up"] = _flag(
        (mtf > 0) & (tf3 > 0) & (tf5 > 0)
    )
    df["flag_multi_tf_resonance"] = _flag(
        (df["flag_mtf_consensus_up"] == 1) &
        ((macd_cross > 0) | (rsi_rebound > 0) | (breakout > 0))
    )

    return df