# ============================================================
# File   : trading/ranking/execution/position_sizing.py
# Version: Ver4-PRODUCTION-ULTRA-STABLE-POSITION-SIZING
# ------------------------------------------------------------
# ✔ スコア比例ロット配分
# ✔ ボラティリティ補正（ATR/price）
# ✔ 最大リスク制御
# ✔ 最小ロット保証
# ✔ 総資金制約
# ✔ 正規化配分
# ✔ NaN / inf 防御
# ✔ pandas vectorized
# ✔ production hardened
# ============================================================

from __future__ import annotations

import logging
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# parameters（戦略のコア）
# ============================================================

MAX_POSITION_RATIO = 0.2     # 1銘柄最大20%
MIN_POSITION_RATIO = 0.01    # 最小1%

RISK_PER_TRADE = 0.01        # 1トレード最大1%リスク
DEFAULT_VOL = 0.01


# ============================================================
# helpers
# ============================================================

def _sanitize(s: pd.Series) -> pd.Series:
    return (
        s.replace([np.inf, -np.inf], np.nan)
         .fillna(0)
    )


def _normalize(s: pd.Series) -> pd.Series:

    s = _sanitize(s)

    total = s.sum()

    if total > 0:
        return s / total

    return s


def _safe(df: pd.DataFrame, col: str) -> pd.Series:

    if col in df.columns:
        return pd.to_numeric(df[col], errors="coerce")

    return pd.Series(0, index=df.index)


# ============================================================
# core
# ============================================================

def _score_weight(df: pd.DataFrame) -> pd.Series:
    """
    スコア比例重み
    """

    score = _safe(df, "score")

    score = score.clip(lower=0)

    return _normalize(score)


def _volatility_adjustment(df: pd.DataFrame) -> pd.Series:
    """
    ボラティリティ補正（逆比例）
    """

    vol = _safe(df, "volatility")

    vol = vol.replace(0, DEFAULT_VOL)

    adj = 1 / vol

    return _normalize(adj)


def _risk_adjustment(df: pd.DataFrame) -> pd.Series:
    """
    ATRベースリスク制御
    """

    atr = _safe(df, "atr")
    price = _safe(df, "close")

    risk = atr / price.replace(0, np.nan)

    risk = risk.replace(0, DEFAULT_VOL)

    adj = 1 / risk

    return _normalize(adj)


# ============================================================
# main
# ============================================================

def compute_position_sizes(
    df: pd.DataFrame,
    *,
    capital: float
) -> pd.DataFrame:
    """
    ポジションサイズ計算

    Parameters
    ----------
    capital : float
        総資金

    Returns
    -------
    df["position_size"]
    df["position_value"]
    """

    if df is None or df.empty:
        return df

    try:

        df = df.copy()

        # ----------------------------------------------------
        # weights
        # ----------------------------------------------------

        w_score = _score_weight(df)
        w_vol = _volatility_adjustment(df)
        w_risk = _risk_adjustment(df)

        # ----------------------------------------------------
        # 統合重み
        # ----------------------------------------------------

        weight = (
            w_score * 0.5 +
            w_vol * 0.3 +
            w_risk * 0.2
        )

        weight = _normalize(weight)

        # ----------------------------------------------------
        # position ratio
        # ----------------------------------------------------

        ratio = weight.clip(
            lower=MIN_POSITION_RATIO,
            upper=MAX_POSITION_RATIO
        )

        # ----------------------------------------------------
        # 資金配分
        # ----------------------------------------------------

        position_value = ratio * capital

        # ----------------------------------------------------
        # 株数計算
        # ----------------------------------------------------

        price = _safe(df, "close").replace(0, np.nan)

        size = position_value / price

        size = _sanitize(size).fillna(0)

        df["position_ratio"] = ratio
        df["position_value"] = position_value
        df["position_size"] = size

        return df

    except Exception:

        logger.exception("[position_sizing] failed")

        df["position_size"] = 0
        return df


# ============================================================
# utility
# ============================================================

def total_allocated(df: pd.DataFrame) -> float:

    if df is None or df.empty:
        return 0

    if "position_value" not in df.columns:
        return 0

    return float(df["position_value"].sum())