# ============================================================
# File   : trading/scoring/flags/relative_strength_flags.py
# Version: Ver1.0-PRODUCTION-RELATIVE-STRENGTH-FLAGS
# ------------------------------------------------------------
# ✔ flag_relative_strength_positive
# ✔ flag_relative_strength_strong
# ✔ flag_market_outperform
# ✔ flag_sector_outperform
# ✔ score / market_return / sector_return ベース
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


def generate_relative_strength_flags(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return df

    df = df.copy()

    score = _safe(_col(df, "final_score", "display_score", "score"))
    market_return = _safe(_col(df, "market_return", "index_return", "nikkei_return"))
    sector_return = _safe(_col(df, "sector_return", "theme_return", "industry_return"))

    if score is None:
        return df

    if market_return is None:
        market_return = pd.Series(0.0, index=df.index)
    if sector_return is None:
        sector_return = pd.Series(0.0, index=df.index)

    rel_market = score - market_return
    rel_sector = score - sector_return

    df["flag_market_outperform"] = _flag(rel_market > 0)
    df["flag_sector_outperform"] = _flag(rel_sector > 0)
    df["flag_relative_strength_positive"] = _flag(
        (rel_market > 0) | (rel_sector > 0)
    )
    df["flag_relative_strength_strong"] = _flag(
        (rel_market > 1.5) | (rel_sector > 1.5)
    )
    df["flag_relative_strength_extreme"] = _flag(
        (rel_market > 3.0) | (rel_sector > 3.0)
    )

    return df