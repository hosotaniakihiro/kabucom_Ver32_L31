# ============================================================
# File   : scheduler_jobs/summary/quality_guards.py
# Ver    : PRODUCTION-STABLE-SUMMARY-QUALITY-GUARDS-V1.0
# ------------------------------------------------------------
# ✔ 未計算ゼロDF判定
# ✔ PUSH / ranking 用 quality guard
# ============================================================

from __future__ import annotations

import logging

import pandas as pd

from .display_prepare import normalize_df, latest_dt_str, symbols_count

logger = logging.getLogger(__name__)


def numeric_sum_abs(df: pd.DataFrame, cols: list[str]) -> float:
    if df is None or df.empty:
        return 0.0

    use_cols = [c for c in cols if c in df.columns]
    if not use_cols:
        return 0.0

    try:
        x = df[use_cols].copy()
        for c in use_cols:
            x[c] = pd.to_numeric(x[c], errors="coerce").fillna(0.0)
        return float(x.abs().sum().sum())
    except Exception:
        return 0.0


def looks_uncomputed_push_df(df: pd.DataFrame) -> bool:
    df = normalize_df(df)
    if df.empty:
        return True

    score_cols = [
        "score", "score_total", "final_score", "display_score",
        "score_buy", "score_sell", "score_slope", "score_mtf",
    ]
    tech_cols = [
        "slope", "slope_atr_scaled", "mtf", "mtf_score",
        "rsi", "macd", "signal", "hist",
    ]

    score_abs = numeric_sum_abs(df, score_cols)
    tech_abs = numeric_sum_abs(df, tech_cols)

    has_price = False
    try:
        if "close" in df.columns:
            has_price = pd.to_numeric(df["close"], errors="coerce").notna().any()
    except Exception:
        has_price = False

    uncomputed = has_price and score_abs == 0.0 and tech_abs == 0.0
    if uncomputed:
        logger.warning(
            "[summary.quality_guards] detected uncomputed push df rows=%s symbols=%s latest_dt=%s",
            len(df),
            symbols_count(df),
            latest_dt_str(df),
        )
    return uncomputed


def looks_uncomputed_ranking_df(df: pd.DataFrame) -> bool:
    df = normalize_df(df)
    if df.empty:
        return True

    cols = [
        "score", "score_total", "final_score",
        "slope", "rsi", "macd", "signal", "hist",
        "best_rank",
    ]
    total_abs = numeric_sum_abs(df, cols)

    has_price = False
    try:
        if "close" in df.columns:
            has_price = pd.to_numeric(df["close"], errors="coerce").notna().any()
    except Exception:
        has_price = False

    uncomputed = has_price and total_abs == 0.0
    if uncomputed:
        logger.warning(
            "[summary.quality_guards] detected uncomputed ranking df rows=%s symbols=%s latest_dt=%s",
            len(df),
            symbols_count(df),
            latest_dt_str(df),
        )
    return uncomputed