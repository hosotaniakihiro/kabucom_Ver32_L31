# ============================================================
# File   : trading/summary/top_candidates_pkg/filters.py
# Version: Ver2.2-PRODUCTION-SUMMARY-TOP-CANDIDATES-FILTERS
# ------------------------------------------------------------
# Function:
#   - ETF / ETN / REIT / FUND 系を可能な範囲で除外
# ============================================================

from __future__ import annotations

import logging

import pandas as pd

logger = logging.getLogger(__name__)


def drop_fund_etf_like(df: pd.DataFrame) -> pd.DataFrame:
    """
    ETF / ETN / REIT / FUND 系を可能な範囲で除外。

    優先:
      - is_etf
      - is_fund
      - is_reit
      - symbolname 文字列判定
    """

    if df is None or df.empty:
        return df

    out = df.copy()

    for col in ["is_etf", "is_fund", "is_reit"]:
        if col in out.columns:
            try:
                out = out[~out[col].fillna(False).astype(bool)]
            except Exception:
                logger.debug("[TOP CANDIDATES] %s filter failed", col, exc_info=True)

    if "symbolname" in out.columns:
        try:
            s = out["symbolname"].astype(str)

            mask = (
                s.str.contains("ETF", case=False, na=False)
                | s.str.contains("ETN", case=False, na=False)
                | s.str.contains("REIT", case=False, na=False)
                | s.str.contains("リート", case=False, na=False)
                | s.str.contains("投信", case=False, na=False)
                | s.str.contains("上場投資信託", case=False, na=False)
                | s.str.contains("ファンド", case=False, na=False)
                | s.str.contains("インデックス", case=False, na=False)
            )

            out = out[~mask].copy()

        except Exception:
            logger.debug("[TOP CANDIDATES] symbolname fund/etf filter failed", exc_info=True)

    return out