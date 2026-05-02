# ============================================================
# File   : trading/scoring/core/filters/output_filter.py
# Version: Ver1.0-PRODUCTION-OUTPUT-FILTER
# ------------------------------------------------------------
# ✔ ETF/ETN/REIT exclusion
# ✔ low liquidity exclusion
# ✔ symbol sanity
# ✔ final display stabilization
# ============================================================

from __future__ import annotations

import logging

import pandas as pd

logger = logging.getLogger(__name__)

_EXCLUDE_NAME_KEYWORDS = [
    "ETF",
    "ETN",
    "REIT",
    "指数",
    "連動",
    "レバ",
    "インバース",
    "ベア",
    "ダブル",
]


def apply_final_output_filter(
    df: pd.DataFrame,
    min_volume: float = 1.0,
    min_turnover: float = 0.0,
) -> pd.DataFrame:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return df

    out = df.copy()

    if "symbol" in out.columns:
        out["symbol"] = out["symbol"].astype(str).str.strip()
        out = out[out["symbol"] != ""].copy()

    if "symbolname" in out.columns:
        name_s = out["symbolname"].astype(str).fillna("")
        mask = pd.Series(True, index=out.index)
        for kw in _EXCLUDE_NAME_KEYWORDS:
            mask &= ~name_s.str.contains(kw, case=False, na=False)
        out = out[mask].copy()

    if "volume" in out.columns:
        out["volume"] = pd.to_numeric(out["volume"], errors="coerce").fillna(0.0)
        out = out[out["volume"] >= float(min_volume)].copy()

    if "close" in out.columns and "volume" in out.columns:
        turnover = (
            pd.to_numeric(out["close"], errors="coerce").fillna(0.0)
            * pd.to_numeric(out["volume"], errors="coerce").fillna(0.0)
        )
        out["turnover"] = turnover
        if float(min_turnover) > 0:
            out = out[out["turnover"] >= float(min_turnover)].copy()

    out = out.reset_index(drop=True)

    logger.info(
        "[SCORING PIPELINE] final output filter rows=%d",
        len(out),
    )
    return out