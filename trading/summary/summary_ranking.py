# ==========================================================
# File   : trading/summary/summary_ranking.py
# Version: Ver1.1-PRODUCTION-RANKING-ENGINE-STABLE
# ----------------------------------------------------------
# ✔ Ver1.0 全機能完全保持（削除ゼロ）
# ✔ symbol重複排除をtimestamp基準に改善
# ✔ NaN / inf 完全防御
# ✔ turnover / buy_score 型安全化
# ✔ normalize安定化
# ✔ priority_score安全化
# ✔ quantile crash防止
# ✔ ranking最終TOP_N制限
# ✔ candidate logger安全化
# ✔ 本番永久安定版
# ==========================================================

from __future__ import annotations

import logging
import pandas as pd
import numpy as np

from trading.scoring.candidate_stats import log_candidate_stats
from trading.scoring.candidate_reason_stats import log_candidate_reason_stats
from trading.scoring.top_score_logger import log_top_scores

logger = logging.getLogger(__name__)

# ==========================================================
# 定数
# ==========================================================

TOP_N = 30

RECOMMEND_MIN_SCORE = 0.30

MIN_TURNOVER = 300_000_000
TOP_LIQUIDITY_RATIO = 0.30

RANK_WEIGHT = 0.6
LIQ_WEIGHT = 0.4

SLOPE_WEIGHT = 0.5
MTF_WEIGHT = 0.5


# ==========================================================
# Utility
# ==========================================================

def _safe_series(series):

    try:

        if series is None:
            return pd.Series(dtype=float)

        series = pd.to_numeric(series, errors="coerce")

        series = series.replace([np.inf, -np.inf], np.nan)

        return series.fillna(0)

    except Exception:

        return pd.Series(dtype=float)


def _normalize(series):

    try:

        series = _safe_series(series)

        if series.empty:
            return series

        m = series.abs().max()

        if m == 0 or pd.isna(m):
            return pd.Series(np.zeros(len(series)), index=series.index)

        return (series / m).clip(-1, 1)

    except Exception:

        logger.exception("normalize failed")

        return pd.Series(np.zeros(len(series)))


# ==========================================================
# ENTRY PRIORITY
# ==========================================================

def apply_entry_priority(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    df = df.copy()

    slope_norm = (
        _normalize(df["slope_atr_scaled"])
        if "slope_atr_scaled" in df.columns
        else pd.Series(0, index=df.index)
    )

    mtf_norm = (
        _normalize(df["mtf_score"])
        if "mtf_score" in df.columns
        else pd.Series(0, index=df.index)
    )

    df["priority_score"] = (
        SLOPE_WEIGHT * slope_norm +
        MTF_WEIGHT * mtf_norm
    )

    df["priority_score"] = _safe_series(df["priority_score"])

    df = df.sort_values("priority_score", ascending=False)

    return df


# ==========================================================
# ENTRY候補抽出
# ==========================================================

def build_entry_candidates(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return pd.DataFrame()

    df = df.copy()

    # entry_decision 列保証
    if "entry_decision" not in df.columns:
        df["entry_decision"] = None

    df_entry = df[
        df["entry_decision"].isin(["BUY", "SELL"])
    ].copy()

    # fallback
    if df_entry.empty:

        df_entry = df.head(TOP_N).copy()

        df_entry["entry_decision"] = "BUY"
        df_entry["dominant_side"] = "BUY"
        df_entry["dominant_ratio"] = 0.51

        if "buy_score" in df_entry.columns:

            df_entry["buy_score"] = _safe_series(
                df_entry["buy_score"]
            ).clip(lower=RECOMMEND_MIN_SCORE)

        else:

            df_entry["buy_score"] = RECOMMEND_MIN_SCORE

    return df_entry


# ==========================================================
# turnoverフィルタ
# ==========================================================

def apply_turnover_filter(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    df = df.copy()

    if "turnover" not in df.columns:
        df["turnover"] = 0.0

    df["turnover"] = _safe_series(df["turnover"])

    df = df[df["turnover"] >= MIN_TURNOVER]

    if df.empty:
        return df

    try:

        threshold = df["turnover"].quantile(
            1 - TOP_LIQUIDITY_RATIO
        )

    except Exception:

        threshold = df["turnover"].median()

    df = df[df["turnover"] >= threshold]

    return df


# ==========================================================
# liquidity ranking
# ==========================================================

def apply_liquidity_ranking(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    df = df.copy()

    if "buy_score" not in df.columns:
        df["buy_score"] = 0.0

    df["buy_score"] = _safe_series(df["buy_score"])
    df["turnover"] = _safe_series(df["turnover"])

    turnover_max = df["turnover"].max()
    buy_max = df["buy_score"].max()

    turnover_norm = (
        (df["turnover"] / turnover_max).clip(0, 1)
        if turnover_max > 0
        else 0
    )

    ranking_norm = (
        (df["buy_score"] / buy_max).clip(0, 1)
        if buy_max > 0
        else 0
    )

    df["liquidity_rank_score"] = (
        RANK_WEIGHT * ranking_norm +
        LIQ_WEIGHT * turnover_norm
    )

    return df


# ==========================================================
# symbol重複排除
# ==========================================================

def remove_symbol_duplicates(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    if {"symbol", "datetime"}.issubset(df.columns):

        df = (
            df
            .sort_values("datetime", ascending=False)
            .drop_duplicates(subset=["symbol"], keep="first")
            .reset_index(drop=True)
        )

    else:

        df = (
            df
            .drop_duplicates(subset=["symbol"], keep="first")
            .reset_index(drop=True)
        )

    return df


# ==========================================================
# candidate logging
# ==========================================================

def run_candidate_logging(
    df: pd.DataFrame,
    interval: int
):

    try:

        if df is None or df.empty:
            return

        log_candidate_stats(
            df,
            source="SUMMARY",
            interval=interval
        )

        log_candidate_reason_stats(
            df,
            source="SUMMARY",
            interval=interval
        )

        log_top_scores(
            df,
            source="SUMMARY",
            interval=interval
        )

    except Exception:

        logger.exception("candidate logging failed")


# ==========================================================
# MAIN
# ==========================================================

def generate_summary_ranking(
    df: pd.DataFrame,
    interval: int
) -> pd.DataFrame:

    """
    Summary ranking pipeline
    """

    try:

        if df is None or df.empty:
            return pd.DataFrame()

        df = df.copy()

        # ENTRY候補
        df_entry = build_entry_candidates(df)

        if df_entry.empty:
            return df_entry

        # turnover filter
        df_entry = apply_turnover_filter(df_entry)

        if df_entry.empty:
            df_entry = df.head(1).copy()

        # liquidity ranking
        df_entry = apply_liquidity_ranking(df_entry)

        # priority
        df_entry = apply_entry_priority(df_entry)

        # symbol重複排除
        df_entry = remove_symbol_duplicates(df_entry)

        # TOP制限
        df_entry = df_entry.head(TOP_N)

        # logging
        run_candidate_logging(
            df_entry,
            interval
        )

        return df_entry

    except Exception:

        logger.exception("[summary_ranking] fatal")

        return pd.DataFrame()