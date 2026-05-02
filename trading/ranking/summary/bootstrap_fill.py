# ============================================================
# File   : trading/ranking/summary/bootstrap_fill.py
# Version: Ver1.0-PRODUCTION-RANKING-SUMMARY-BOOTSTRAP-FILL
# ------------------------------------------------------------
# 【概要】
#   ranking summary を PUSH由来 summary で補完
#
# 【重要】
#   - PUSH summary DB は読むだけ
#   - ranking OHLC は最終的に close 同値へ戻す
# ============================================================

from __future__ import annotations

import logging

import pandas as pd

from trading.ranking.summary.bootstrap_ohlcv import (
    normalize_datetime,
    normalize_symbol,
)

logger = logging.getLogger(__name__)


FILL_COLUMNS = [
    "open",
    "high",
    "low",
    "close",
    "volume",
    "ma5",
    "ma25",
    "ma75",
    "rsi",
    "macd",
    "signal",
    "hist",
    "atr",
    "vwap",
    "slope",
    "slope_atr_scaled",
    "score",
    "score_total",
    "final_score",
    "display_score",
    "score_buy",
    "score_sell",
    "score_slope",
    "score_mtf",
]


TECHNICAL_FILL_COLUMNS = [
    "ma5",
    "ma25",
    "ma75",
    "rsi",
    "macd",
    "signal",
    "hist",
    "atr",
    "vwap",
    "slope",
    "slope_atr_scaled",
]


SCORE_FILL_COLUMNS = [
    "score",
    "score_total",
    "final_score",
    "display_score",
    "score_buy",
    "score_sell",
    "score_slope",
    "score_mtf",
]


def merge_push_summary_fill(
    ranking_df: pd.DataFrame,
    push_df: pd.DataFrame,
    *,
    interval: int,
) -> pd.DataFrame:
    if ranking_df is None or ranking_df.empty:
        return pd.DataFrame()

    if push_df is None or push_df.empty:
        return ranking_df.copy()

    x = ranking_df.copy()
    p = push_df.copy()

    x = normalize_symbol(x)
    x = normalize_datetime(x)
    p = normalize_symbol(p)
    p = normalize_datetime(p)

    if x.empty or p.empty:
        return x

    p = p.drop_duplicates(subset=["symbol", "datetime"], keep="last").copy()

    keep = ["symbol", "datetime"] + [c for c in FILL_COLUMNS if c in p.columns]
    p = p[keep].copy()

    merged = pd.merge(
        x,
        p,
        on=["symbol", "datetime"],
        how="left",
        suffixes=("", "_push"),
    )

    for c in FILL_COLUMNS:
        alt = f"{c}_push"

        if alt not in merged.columns:
            continue

        try:
            if c not in merged.columns:
                merged[c] = merged[alt]
            else:
                left = pd.to_numeric(merged[c], errors="coerce")
                right = pd.to_numeric(merged[alt], errors="coerce")

                if c == "volume":
                    merged[c] = left.where(left.fillna(0).ne(0), right)
                elif c in TECHNICAL_FILL_COLUMNS:
                    merged[c] = left.combine_first(right)
                elif c in SCORE_FILL_COLUMNS:
                    merged[c] = left.where(left.fillna(0).ne(0), right)
                else:
                    merged[c] = left.combine_first(right)

            merged.drop(columns=[alt], inplace=True, errors="ignore")

        except Exception:
            logger.exception(
                "[RANKING SUMMARY BOOTSTRAP FILL] fill failed interval=%s col=%s",
                interval,
                c,
            )
            merged.drop(columns=[alt], inplace=True, errors="ignore")

    # OHLC 同値方針を維持
    if "close" in merged.columns:
        close = pd.to_numeric(merged["close"], errors="coerce")
        merged["open"] = close
        merged["high"] = close
        merged["low"] = close
        merged["close"] = close

    logger.info(
        "[RANKING SUMMARY BOOTSTRAP FILL] interval=%s ranking_rows=%d push_rows=%d out_rows=%d",
        interval,
        len(ranking_df),
        len(push_df),
        len(merged),
    )

    return merged


__all__ = [
    "FILL_COLUMNS",
    "TECHNICAL_FILL_COLUMNS",
    "SCORE_FILL_COLUMNS",
    "merge_push_summary_fill",
]