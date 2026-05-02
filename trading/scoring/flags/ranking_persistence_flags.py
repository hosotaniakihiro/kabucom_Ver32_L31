# ============================================================
# File   : trading/scoring/flags/ranking_persistence_flags.py
# Version: Ver1.0-PRODUCTION-RANKING-PERSISTENCE-FLAGS
# ------------------------------------------------------------
# ✔ flag_ranking_good
# ✔ flag_ranking_improving
# ✔ flag_ranking_persistent
# ✔ flag_ranking_reaccel
# ✔ ranking_position / ranking_score / best_rank 対応
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
        return pd.Series(0, index=expr.index)


def generate_ranking_persistence_flags(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return df

    df = df.copy()

    rank = _safe(_col(df, "ranking_position", "best_rank", "rank", "ranking_rank"))
    ranking_score = _safe(_col(df, "ranking_score", "rank_score"))

    if rank is None and ranking_score is None:
        return df

    if rank is None:
        rank = pd.Series(999.0, index=df.index)
    if ranking_score is None:
        ranking_score = pd.Series(0.0, index=df.index)

    df["flag_ranking_good"] = _flag(rank <= 20)
    df["flag_ranking_improving"] = _flag(rank < rank.shift(1))
    df["flag_ranking_persistent"] = _flag(
        (df["flag_ranking_good"].rolling(3, min_periods=1).sum() >= 2)
    )
    df["flag_ranking_reaccel"] = _flag(
        (df["flag_ranking_good"] == 1) &
        (ranking_score > ranking_score.shift(1))
    )
    df["flag_ranking_top10"] = _flag(rank <= 10)

    return df