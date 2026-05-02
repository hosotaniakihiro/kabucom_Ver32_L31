# ============================================================
# File   : trading/ranking/scoring/score_builder.py
# Version: Ver4.2-FULL-COMPAT-PRODUCTION-FIXED
# ------------------------------------------------------------
# ✔ Ver4.1 完全保持
# ✔ velocityゼロ問題修正（最重要）
# ✔ acceleration groupby修正
# ✔ normalizeロバスト化
# ✔ price fallback追加
# ✔ スコア安定化（非破壊）
# ✔ NEW: stable sort before history-based calculations
# ✔ NEW: ranking summary display columns guarantee
# ============================================================

from __future__ import annotations

import logging
import numpy as np
import pandas as pd

from trading.ranking.scoring.fallback_score import (
    calculate_fallback_score,
    ensure_fallback_display_columns,
)
from trading.ranking.scoring.weight_config import get_weight_set

logger = logging.getLogger(__name__)


# ============================================================
# helpers（完全保持＋強化）
# ============================================================

def _safe_series(df: pd.DataFrame) -> pd.Series:
    return pd.Series(0.0, index=df.index)


def _sanitize_numeric(series: pd.Series) -> pd.Series:
    try:
        return (
            pd.to_numeric(series, errors="coerce")
            .replace([np.inf, -np.inf], 0)
            .fillna(0)
        )
    except Exception:
        return pd.Series(0, index=series.index if hasattr(series, "index") else None)


def _normalize(series: pd.Series) -> pd.Series:
    series = _sanitize_numeric(series)

    p = np.percentile(np.abs(series), 95) if len(series) else 0

    if p > 0:
        series = series / (p + 1e-9)

    return series.clip(-1, 1)


def _tanh(series: pd.Series, scale: float = 1.0) -> pd.Series:
    return np.tanh(_sanitize_numeric(series) * scale)


def _get(df: pd.DataFrame, col: str) -> pd.Series:
    try:
        if col in df.columns:
            s = df[col]
            if len(s) == len(df):
                return _sanitize_numeric(s)
    except Exception:
        pass
    return _safe_series(df)


def _stable_sort(df: pd.DataFrame) -> pd.DataFrame:
    try:
        out = df.copy()
        if "symbol" not in out.columns:
            return out
        if "datetime" in out.columns:
            out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
            out = out.sort_values(["symbol", "datetime"], kind="stable")
        else:
            out = out.sort_values(["symbol"], kind="stable")
        return out.reset_index(drop=True)
    except Exception:
        logger.exception("[score_builder] stable sort failed")
        return df.copy()


# ============================================================
# feature extraction（完全保持）
# ============================================================

def _ensure_slope(df: pd.DataFrame) -> pd.Series:
    if "score_slope" in df.columns:
        return _sanitize_numeric(df["score_slope"])
    if "ma25_slope" in df.columns:
        return _sanitize_numeric(df["ma25_slope"])
    if "slope" in df.columns:
        return _sanitize_numeric(df["slope"])
    return _safe_series(df)


def _ensure_mtf(df: pd.DataFrame) -> pd.Series:
    if "score_mtf" in df.columns:
        return _sanitize_numeric(df["score_mtf"])
    if "ma25" in df.columns and "ma75" in df.columns:
        return (pd.to_numeric(df["ma25"], errors="coerce") > pd.to_numeric(df["ma75"], errors="coerce")).astype(float)
    return _safe_series(df)


def _ensure_momentum(df: pd.DataFrame) -> pd.Series:
    if "mom" in df.columns:
        return _sanitize_numeric(df["mom"])
    if "momentum" in df.columns:
        return _sanitize_numeric(df["momentum"])
    if "ret5" in df.columns:
        return _sanitize_numeric(df["ret5"])
    return _safe_series(df)


def _ai_scores(df: pd.DataFrame) -> pd.Series:
    score = _safe_series(df)

    if "smart_money_score" in df.columns:
        score += _tanh(df["smart_money_score"], 1.5) * 3

    if "ignition_score" in df.columns:
        score += _tanh(df["ignition_score"], 1.5) * 5

    if "entry_timing_score" in df.columns:
        score += _tanh(df["entry_timing_score"], 1.5) * 2

    return _sanitize_numeric(score)


# ============================================================
# 🔥 velocity（修正版）
# ============================================================

def _price_velocity(df: pd.DataFrame) -> pd.Series:
    col = "price" if "price" in df.columns else "close"

    if col not in df.columns or "symbol" not in df.columns:
        return _safe_series(df)

    s = (
        pd.to_numeric(df[col], errors="coerce")
        .groupby(df["symbol"])
        .diff()
        .rolling(5, min_periods=1)
        .mean()
    )

    return _sanitize_numeric(s)


def _volume_velocity(df: pd.DataFrame) -> pd.Series:
    if "volume" not in df.columns or "symbol" not in df.columns:
        return _safe_series(df)

    s = (
        pd.to_numeric(df["volume"], errors="coerce")
        .groupby(df["symbol"])
        .diff()
        .rolling(5, min_periods=1)
        .mean()
    )

    return _sanitize_numeric(s)


# ============================================================
# main
# ============================================================

def build_ranking_score(df: pd.DataFrame, *, regime: str | None = None) -> pd.DataFrame:
    if df is None or not isinstance(df, pd.DataFrame):
        return pd.DataFrame()

    if df.empty:
        return df.copy()

    try:
        df = _stable_sort(df.copy())

        # ----------------------------------------------------
        # base
        # ----------------------------------------------------
        if "score_buy" in df.columns:
            base_score = _sanitize_numeric(df["score_buy"])
        else:
            base_score, slope_tmp = calculate_fallback_score(df)
            base_score = _sanitize_numeric(base_score)
            df["score_slope"] = slope_tmp
            if "slope" not in df.columns:
                df["slope"] = slope_tmp

        # ----------------------------------------------------
        # features
        # ----------------------------------------------------
        slope = _ensure_slope(df)
        mtf = _ensure_mtf(df)
        momentum = _ensure_momentum(df)

        velocity_rank = _get(df, "ranking_velocity")
        volume = _get(df, "volume")

        ignition = _get(df, "ignition_score")
        smart_money = _get(df, "smart_money_score")
        institutional = _get(df, "institutional_score")

        ai_score = _ai_scores(df)

        # ====================================================
        # velocity（修正版）
        # ====================================================
        price_vel = _price_velocity(df)
        volume_vel = _volume_velocity(df)

        velocity = (
            price_vel * 0.7
            + volume_vel * 0.2
            + velocity_rank * 0.1
        )

        # ====================================================
        # layers（完全保持）
        # ====================================================
        base_layer = _normalize(volume + _get(df, "turnover"))

        trend_layer = _normalize(_tanh(slope * 2, 1.5))
        momentum_layer = _normalize(_tanh(momentum, 1.2))
        velocity_layer = _normalize(_tanh(velocity * 3))

        event_layer = _normalize(_tanh(ignition, 1.5))
        flow_layer = _normalize(_tanh(smart_money + institutional * 0.5, 1.5))

        # ====================================================
        # acceleration（修正）
        # ====================================================
        if "symbol" in df.columns:
            acceleration = (
                momentum.groupby(df["symbol"])
                .diff()
                .groupby(df["symbol"])
                .diff()
                .fillna(0)
            )
        else:
            acceleration = _safe_series(df)

        # ====================================================
        # synergy / early（完全保持）
        # ====================================================
        synergy = (
            (momentum_layer > 0.5)
            & (velocity_layer > 0.3)
            & (trend_layer > 0.2)
        ).astype(float)

        early = (
            (velocity_layer > 0.6)
            & (momentum_layer > 0.6)
        ).astype(float)

        weights = get_weight_set(regime)

        # ====================================================
        # score
        # ====================================================
        acceleration_layer = _normalize(acceleration)

        layered_score = (
            base_layer * weights.base
            + trend_layer * weights.trend
            + momentum_layer * weights.momentum
            + velocity_layer * weights.velocity
            + acceleration_layer * weights.acceleration
            + event_layer * weights.event
            + flow_layer * weights.flow
        )

        layered_score += synergy * 2.0
        layered_score += early * 2.5

        legacy_score = (
            base_score
            + slope * 2
            + mtf
            + ai_score * 0.5
        )

        score = layered_score * 0.9 + legacy_score * 0.1

        score = _sanitize_numeric(score)

        # ★ 安定化（非破壊）
        score = np.tanh(score / 5) * 10

        df["score"] = score

        # ----------------------------------------------------
        # debug（完全保持）
        # ----------------------------------------------------
        df["_score_base"] = base_layer
        df["_score_trend"] = trend_layer
        df["_score_momentum"] = momentum_layer
        df["_score_velocity"] = velocity_layer
        df["_score_event"] = event_layer
        df["_score_flow"] = flow_layer
        df["_score_acceleration"] = acceleration
        df["_score_synergy"] = synergy
        df["_score_early"] = early

        df = ensure_fallback_display_columns(df)

        return df

    except Exception:
        logger.exception("[score_builder] failed")
        return pd.DataFrame()
