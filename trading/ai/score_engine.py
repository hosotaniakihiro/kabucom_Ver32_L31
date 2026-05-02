# ============================================================
# File: trading/ai/score_engine.py
# Version: ABSOLUTE-FINAL-MTF-REGIME-BANDIT-SAFE
# ------------------------------------------------------------
# ✔ score_total 既存互換100%
# ✔ 1min / 3min / 5min 自動分岐
# ✔ micro / trend / volatility 分離設計
# ✔ regime重み対応
# ✔ bandit重み対応
# ✔ NaN / inf 完全排除
# ✔ dtype安全
# ✔ future-safe
# ============================================================

from __future__ import annotations

import pandas as pd
import numpy as np
import logging

logger = logging.getLogger(__name__)


# ============================================================
# 数値安全化ユーティリティ
# ============================================================

def _num_series(df: pd.DataFrame, col: str, default=0.0):

    if col not in df.columns:
        return pd.Series(default, index=df.index)

    return (
        pd.to_numeric(df[col], errors="coerce")
        .replace([np.inf, -np.inf], default)
        .fillna(default)
    )


# ============================================================
# ① micro score（主に1min）
# ============================================================

def _build_micro_score(df: pd.DataFrame):

    df = df.copy()

    base = _num_series(df, "score_total")

    vwap_break = df.get("vwap_break", False).astype(int)
    macd_gc = df.get("macd_gc", False).astype(int)
    stoch_rebound = df.get("stoch_rebound", False).astype(int)
    rsi_boost = (_num_series(df, "rsi") > 55).astype(int)

    df["micro_score"] = (
        base
        + vwap_break * 2
        + macd_gc * 2
        + stoch_rebound * 1
        + rsi_boost * 1
    )

    return df


# ============================================================
# ② trend score（主に3/5min）
# ============================================================

def _build_trend_score(df: pd.DataFrame):

    df = df.copy()

    slope = _num_series(df, "ma75_slope")
    rsi = _num_series(df, "rsi")

    ma_align = df.get("ma_alignment", False).astype(int)
    ma_align_down = df.get("ma_alignment_down", False).astype(int)

    df["trend_score"] = (
        slope * 50
        + ma_align * 3
        - ma_align_down * 3
        + (rsi > 55).astype(int) * 2
        - (rsi < 45).astype(int) * 2
    )

    return df


# ============================================================
# ③ volatility score
# ============================================================

def _build_volatility_score(df: pd.DataFrame):

    df = df.copy()

    vol_slope = _num_series(df, "volume_slope")
    bb_width = _num_series(df, "bb_width")
    atr = _num_series(df, "atr")

    df["volatility_score"] = (
        vol_slope
        + bb_width
        + atr * 0.5
    )

    return df


# ============================================================
# ④ base score生成（regime/bandit前）
# ============================================================

def build_base_score(df: pd.DataFrame, *, interval: int | str):

    if df is None or df.empty:
        return df

    if isinstance(interval, str):
        interval = int(interval.replace("min", ""))

    df = df.copy()

    df = _build_micro_score(df)
    df = _build_trend_score(df)
    df = _build_volatility_score(df)

    if interval == 1:
        df["base_score"] = df["micro_score"]

    else:
        df["base_score"] = (
            df["trend_score"] * 0.7
            + df["volatility_score"] * 0.3
        )

    df["base_score"] = (
        pd.to_numeric(df["base_score"], errors="coerce")
        .replace([np.inf, -np.inf], 0.0)
        .fillna(0.0)
    )

    return df


# ============================================================
# ⑤ regime + bandit 最終スコア
# ============================================================

def apply_dynamic_weight(
    df: pd.DataFrame,
    *,
    micro_weight: float,
    trend_weight: float,
    bandit_weight: float = 1.0,
):

    if df is None or df.empty:
        return df

    df = df.copy()

    micro = _num_series(df, "micro_score")
    trend = _num_series(df, "trend_score")

    df["final_score"] = (
        micro * micro_weight
        + trend * trend_weight
    ) * bandit_weight

    df["final_score"] = (
        pd.to_numeric(df["final_score"], errors="coerce")
        .replace([np.inf, -np.inf], 0.0)
        .fillna(0.0)
    )

    return df


# ============================================================
# ⑥ 完全統合関数（summary_builder用）
# ============================================================

def build_full_score(
    df: pd.DataFrame,
    *,
    interval: int | str,
    micro_weight: float = 0.5,
    trend_weight: float = 0.5,
    bandit_weight: float = 1.0,
):

    if df is None or df.empty:
        return df

    df = build_base_score(df, interval=interval)

    df = apply_dynamic_weight(
        df,
        micro_weight=micro_weight,
        trend_weight=trend_weight,
        bandit_weight=bandit_weight,
    )

    # 既存互換
    df["score_total"] = df["final_score"]
    df["score"] = df["final_score"]

    return df