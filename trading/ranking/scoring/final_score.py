# ============================================================
# File   : trading/ranking/scoring/final_score.py
# Version: Ver3.1-PRODUCTION-ULTRA-FINAL-SCORE-FIXED
# ------------------------------------------------------------
# ✔ レイヤー分離型スコア統合
# ✔ fallback完全統合
# ✔ trend / momentum / event / flow 分離
# ✔ normalize（銘柄間比較可能）
# ✔ NaN / inf 完全防御
# ✔ score clipping
# ✔ 拡張可能設計（AI置換対応）
# ✔ pandas vectorized
# ✔ production hardened
# ✔ NEW: display columns guarantee for ranking summary
# ============================================================

from __future__ import annotations

import logging
import numpy as np
import pandas as pd

from trading.ranking.scoring.fallback_score import (
    calculate_fallback_score,
    ensure_fallback_display_columns,
)

logger = logging.getLogger(__name__)


# ============================================================
# weights（ここが戦略）
# ============================================================

WEIGHTS = {
    "base": 0.40,
    "trend": 0.25,
    "momentum": 0.15,
    "event": 0.10,
    "flow": 0.10,
}

MAX_SCORE = 1000
MIN_SCORE = -1000


# ============================================================
# helpers
# ============================================================

def _sanitize(s: pd.Series) -> pd.Series:
    return (
        pd.to_numeric(s, errors="coerce")
        .replace([np.inf, -np.inf], np.nan)
        .fillna(0)
    )


def _normalize(s: pd.Series) -> pd.Series:
    s = _sanitize(s)
    max_abs = s.abs().max()

    if max_abs > 0:
        return s / max_abs

    return s


def _get(df: pd.DataFrame, col: str) -> pd.Series:
    if col in df.columns:
        return _sanitize(df[col])
    return pd.Series(0, index=df.index)


# ============================================================
# layer builders
# ============================================================

def _build_base(df: pd.DataFrame) -> pd.Series:
    base, slope = calculate_fallback_score(df)
    df["score_slope"] = slope
    if "slope" not in df.columns:
        df["slope"] = slope
    return _normalize(base)


def _build_trend(df: pd.DataFrame) -> pd.Series:
    mtf = _get(df, "score_mtf")
    slope = _get(df, "score_slope")

    trend = mtf * 0.6 + slope * 0.4

    return _normalize(trend)


def _build_momentum(df: pd.DataFrame) -> pd.Series:
    velocity = _get(df, "ranking_velocity")
    return _normalize(velocity)


def _build_event(df: pd.DataFrame) -> pd.Series:
    ignition = _get(df, "ignition_score")
    return _normalize(ignition)


def _build_flow(df: pd.DataFrame) -> pd.Series:
    smart = _get(df, "smart_money_score")
    inst = _get(df, "institutional_score")

    flow = smart * 0.7 + inst * 0.3

    return _normalize(flow)


# ============================================================
# main
# ============================================================

def build_final_score(
    df: pd.DataFrame
) -> pd.DataFrame:
    """
    最終ランキングスコア生成

    出力:
        df["score"]
    """

    if df is None:
        return pd.DataFrame()

    if not isinstance(df, pd.DataFrame):
        return pd.DataFrame()

    if df.empty:
        return df.copy()

    try:
        df = df.copy()

        # ----------------------------------------------------
        # layers
        # ----------------------------------------------------
        base = _build_base(df)
        trend = _build_trend(df)
        momentum = _build_momentum(df)
        event = _build_event(df)
        flow = _build_flow(df)

        # ----------------------------------------------------
        # weighted sum
        # ----------------------------------------------------
        score = (
            base * WEIGHTS["base"]
            + trend * WEIGHTS["trend"]
            + momentum * WEIGHTS["momentum"]
            + event * WEIGHTS["event"]
            + flow * WEIGHTS["flow"]
        )

        score = _sanitize(score)

        # ----------------------------------------------------
        # clipping
        # ----------------------------------------------------
        score = score.clip(MIN_SCORE, MAX_SCORE)

        df["score"] = score

        # ----------------------------------------------------
        # debug columns（重要）
        # ----------------------------------------------------
        df["_score_base"] = base
        df["_score_trend"] = trend
        df["_score_momentum"] = momentum
        df["_score_event"] = event
        df["_score_flow"] = flow

        # ----------------------------------------------------
        # ranking summary display columns
        # ----------------------------------------------------
        df = ensure_fallback_display_columns(df)

        return df

    except Exception:
        logger.exception("[final_score] failed")
        df = df.copy()
        df["score"] = 0
        return ensure_fallback_display_columns(df)
