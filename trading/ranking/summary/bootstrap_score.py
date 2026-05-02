# ============================================================
# File   : trading/ranking/summary/bootstrap_score.py
# Version: Ver1.0-PRODUCTION-RANKING-SUMMARY-BOOTSTRAP-SCORE
# ------------------------------------------------------------
# 【概要】
#   ranking summary 用 score 列補完
#
# 【重要】
#   pd.Series(pd.NA, dtype="float64") は使わない
# ============================================================

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from trading.ranking.summary.bootstrap_loader import resolve_callable
from trading.ranking.summary.bootstrap_ohlcv import safe_numeric_series

logger = logging.getLogger(__name__)


SCORE_COLUMNS = [
    "score",
    "score_total",
    "final_score",
    "display_score",
    "score_buy",
    "score_sell",
    "score_slope",
    "score_mtf",
]


def ensure_score_columns_fallback(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame() if df is None else df.copy()

    x = df.copy()

    rank = safe_numeric_series(x, "best_rank_position", default=np.nan)
    tick = safe_numeric_series(x, "tick_count", default=0.0, fill=True)
    trading_value = safe_numeric_series(x, "trading_value", default=0.0, fill=True)
    slope = safe_numeric_series(x, "slope", default=0.0, fill=True)
    hist = safe_numeric_series(x, "hist", default=0.0, fill=True)

    rank_score = (51.0 - rank.clip(lower=1, upper=50)).fillna(0.0) / 5.0
    tick_score = np.log1p(tick.clip(lower=0.0)) / 10.0
    value_score = np.log1p(trading_value.clip(lower=0.0)) / 12.0
    tech_score = slope.clip(-5, 5) + hist.clip(-5, 5)

    base = (
        pd.to_numeric(rank_score, errors="coerce").fillna(0.0)
        + pd.to_numeric(tick_score, errors="coerce").fillna(0.0)
        + pd.to_numeric(value_score, errors="coerce").fillna(0.0)
        + pd.to_numeric(tech_score, errors="coerce").fillna(0.0)
    ).astype("float64")

    if "score" in x.columns:
        existing = safe_numeric_series(x, "score", default=0.0, fill=True)
        x["score"] = existing.where(existing.ne(0), base)
    else:
        x["score"] = base

    if "score_total" in x.columns:
        existing = safe_numeric_series(x, "score_total", default=0.0, fill=True)
        x["score_total"] = existing.where(existing.ne(0), x["score"])
    else:
        x["score_total"] = x["score"]

    if "final_score" in x.columns:
        existing = safe_numeric_series(x, "final_score", default=0.0, fill=True)
        x["final_score"] = existing.where(existing.ne(0), x["score_total"])
    else:
        x["final_score"] = x["score_total"]

    if "display_score" in x.columns:
        existing = safe_numeric_series(x, "display_score", default=0.0, fill=True)
        x["display_score"] = existing.where(existing.ne(0), x["final_score"])
    else:
        x["display_score"] = x["final_score"]

    if "score_buy" in x.columns:
        x["score_buy"] = safe_numeric_series(x, "score_buy", default=0.0, fill=True)
    else:
        x["score_buy"] = pd.to_numeric(x["final_score"], errors="coerce").fillna(0.0).clip(lower=0.0)

    if "score_sell" in x.columns:
        x["score_sell"] = safe_numeric_series(x, "score_sell", default=0.0, fill=True)
    else:
        x["score_sell"] = (-pd.to_numeric(x["final_score"], errors="coerce").fillna(0.0)).clip(lower=0.0)

    if "score_slope" in x.columns:
        x["score_slope"] = safe_numeric_series(x, "score_slope", default=0.0, fill=True)
    else:
        x["score_slope"] = slope

    if "score_mtf" in x.columns:
        x["score_mtf"] = safe_numeric_series(x, "score_mtf", default=0.0, fill=True)
    else:
        x["score_mtf"] = pd.Series(0.0, index=x.index, dtype="float64")

    for c in SCORE_COLUMNS:
        x[c] = pd.to_numeric(x[c], errors="coerce").fillna(0.0).astype("float64")

    logger.info(
        "[RANKING SUMMARY BOOTSTRAP SCORE] fallback ensured rows=%d score_nonzero=%d buy_nonzero=%d sell_nonzero=%d",
        len(x),
        int(pd.to_numeric(x["score"], errors="coerce").fillna(0).ne(0).sum()),
        int(pd.to_numeric(x["score_buy"], errors="coerce").fillna(0).ne(0).sum()),
        int(pd.to_numeric(x["score_sell"], errors="coerce").fillna(0).ne(0).sum()),
    )

    return x


def ensure_score_columns(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame() if df is None else df.copy()

    fn = resolve_callable(
        [
            ("trading.ranking.summary.score", "ensure_score_columns"),
            ("trading.ranking.summary.score", "ensure_ranking_score_columns"),
            ("trading.ranking.summary.score", "apply_score_columns"),
        ]
    )

    if callable(fn):
        try:
            out = fn(df.copy())
            if isinstance(out, pd.DataFrame):
                return out
        except Exception:
            logger.exception("[RANKING SUMMARY BOOTSTRAP SCORE] existing score failed -> fallback")

    return ensure_score_columns_fallback(df)


__all__ = [
    "SCORE_COLUMNS",
    "ensure_score_columns_fallback",
    "ensure_score_columns",
]