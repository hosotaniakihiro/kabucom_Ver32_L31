# ============================================================
# File   : trading/ai/pullback_entry_ai.py
# Version: Ver2.0-PRO-PULLBACK-ENTRY-AI-PRODUCTION
# ------------------------------------------------------------
# ✔ 急騰株 pullback entry detector
# ✔ VWAP bounce
# ✔ MA25 bounce
# ✔ shallow pullback
# ✔ volume contraction
# ✔ momentum continuation
# ✔ ranking strength
# ✔ NaN / inf 完全防御
# ✔ vectorized
# ✔ production safe
# ✔ dataframe copy safe
# ✔ column drift tolerance
# ============================================================

from __future__ import annotations

import logging
import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)


# ============================================================
# safe numeric
# ============================================================

def _safe(df: pd.DataFrame, col: str, default: float = 0.0) -> pd.Series:

    try:

        if col not in df.columns:
            return pd.Series(default, index=df.index)

        s = pd.to_numeric(df[col], errors="coerce")

        s = s.replace([np.inf, -np.inf], np.nan)

        return s.fillna(default)

    except Exception:

        return pd.Series(default, index=df.index)


# ============================================================
# shallow pullback detection
# ============================================================

def _pullback_depth(df: pd.DataFrame) -> pd.DataFrame:

    close = _safe(df, "close")
    high = _safe(df, "high")

    rolling_high = high.rolling(20, min_periods=1).max()

    depth = (rolling_high - close) / (rolling_high + 1e-9)

    df["pullback_depth"] = depth.clip(lower=0)

    return df


# ============================================================
# VWAP bounce
# ============================================================

def _vwap_bounce(df: pd.DataFrame) -> pd.DataFrame:

    close = _safe(df, "close")
    vwap = _safe(df, "vwap")

    df["pullback_vwap"] = (close > vwap).astype(int)

    return df


# ============================================================
# MA bounce
# ============================================================

def _ma_bounce(df: pd.DataFrame) -> pd.DataFrame:

    close = _safe(df, "close")
    ma = _safe(df, "ma25")

    df["pullback_ma"] = (close > ma).astype(int)

    return df


# ============================================================
# volume contraction
# ============================================================

def _volume_contraction(df: pd.DataFrame) -> pd.DataFrame:

    volume = _safe(df, "volume")

    vol_ma = volume.rolling(10, min_periods=1).mean()

    ratio = volume / (vol_ma + 1e-9)

    ratio = ratio.replace([np.inf, -np.inf], 0)

    df["pullback_volume"] = ratio.clip(upper=5)

    return df


# ============================================================
# momentum continuation
# ============================================================

def _momentum(df: pd.DataFrame) -> pd.DataFrame:

    close = _safe(df, "close")

    mom = close.diff()

    mom = mom.replace([np.inf, -np.inf], 0).fillna(0)

    df["pullback_momentum"] = mom

    return df


# ============================================================
# ranking support
# ============================================================

def _ranking_support(df: pd.DataFrame) -> pd.DataFrame:

    possible_cols = [
        "ranking_score",
        "rank_score",
        "ranking_strength",
        "score",
    ]

    col = None

    for c in possible_cols:
        if c in df.columns:
            col = c
            break

    if col is None:

        df["pullback_rank"] = 0

        return df

    score = _safe(df, col)

    df["pullback_rank"] = score

    return df


# ============================================================
# pullback score
# ============================================================

def _pullback_score(df: pd.DataFrame) -> pd.DataFrame:

    df["pullback_score"] = (
        (1 - df["pullback_depth"]) * 30
        + df["pullback_vwap"] * 20
        + df["pullback_ma"] * 20
        + df["pullback_volume"] * 10
        + df["pullback_momentum"] * 10
        + df["pullback_rank"] * 0.1
    )

    df["pullback_score"] = df["pullback_score"].replace([np.inf, -np.inf], 0)

    return df


# ============================================================
# main
# ============================================================

def apply_pullback_entry_ai(df: pd.DataFrame) -> pd.DataFrame:

    try:

        if df is None or len(df) == 0:
            return df

        df = df.copy()

        df = _pullback_depth(df)

        df = _vwap_bounce(df)

        df = _ma_bounce(df)

        df = _volume_contraction(df)

        df = _momentum(df)

        df = _ranking_support(df)

        df = _pullback_score(df)

        return df

    except Exception:

        logger.exception("[pullback_entry_ai] failed")

        return df


# ============================================================
# candidate filter
# ============================================================

def detect_pullback_candidates(df: pd.DataFrame) -> pd.DataFrame:

    try:

        if df is None or len(df) == 0:
            return pd.DataFrame()

        df = apply_pullback_entry_ai(df)

        cond = (
            (df["pullback_depth"] < 0.04)
            & (df["pullback_vwap"] == 1)
            & (df["pullback_volume"] < 1.5)
            & (df["pullback_score"] > 25)
        )

        candidates = df.loc[cond].copy()

        return candidates

    except Exception:

        logger.exception("[pullback_entry_ai] detection failed")

        return pd.DataFrame()