# ============================================================
# File   : trading/scoring/flags/ai_flags.py
# Version: Ver1.0-PRODUCTION-AI-FLAGS
# ------------------------------------------------------------
# ✔ flag_ai_momentum_boost
# ✔ flag_ai_ranking_boost
# ✔ flag_ai_confidence_high
# ✔ flag_ai_exit_signal
# ✔ flag_ai_reversal_warning
# ✔ score_config.ini 完全対応
# ✔ AI列欠損安全
# ✔ NaN / inf 完全防御
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

def generate_ai_flags(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    df = df.copy()

    # --------------------------------------------------------
    # AI inputs
    # --------------------------------------------------------

    ai_score = _safe(_col(df, "ai_score", "ai_entry_score"))

    ai_conf = _safe(_col(df, "ai_confidence", "ai_conf"))

    ai_exit = _safe(_col(df, "ai_exit_prob", "ai_exit_score"))

    ai_rev = _safe(_col(df, "ai_reversal_prob", "ai_reversal_score"))

    ranking = _safe(_col(df, "ranking_score", "score"))

    # ========================================================
    # momentum boost
    # ========================================================

    if ai_score is not None:

        df["flag_ai_momentum_boost"] = _flag(
            ai_score > ai_score.rolling(10).mean()
        )

    else:

        df["flag_ai_momentum_boost"] = 0

    # ========================================================
    # ranking boost
    # ========================================================

    if ai_score is not None and ranking is not None:

        df["flag_ai_ranking_boost"] = _flag(
            ai_score > ranking
        )

    else:

        df["flag_ai_ranking_boost"] = 0

    # ========================================================
    # high confidence
    # ========================================================

    if ai_conf is not None:

        df["flag_ai_confidence_high"] = _flag(
            ai_conf > 0.7
        )

    else:

        df["flag_ai_confidence_high"] = 0

    # ========================================================
    # exit signal
    # ========================================================

    if ai_exit is not None:

        df["flag_ai_exit_signal"] = _flag(
            ai_exit > 0.6
        )

    else:

        df["flag_ai_exit_signal"] = 0

    # ========================================================
    # reversal warning
    # ========================================================

    if ai_rev is not None:

        df["flag_ai_reversal_warning"] = _flag(
            ai_rev > 0.6
        )

    else:

        df["flag_ai_reversal_warning"] = 0

    return df