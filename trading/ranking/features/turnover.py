# ============================================================
# File   : trading/ranking/features/turnover.py
# Version: Ver3-PRODUCTION-ULTRA-STABLE-TURNOVER
# ------------------------------------------------------------
# ✔ turnover生成（volume × close）
# ✔ price alias対応（close / price / CurrentPrice）
# ✔ NaN / inf 完全防御
# ✔ dtype安全化
# ✔ negative値防止
# ✔ 異常値クリップ
# ✔ 既存turnover尊重
# ✔ pandas alignment crash防止
# ✔ production hardened
# ============================================================

from __future__ import annotations

import logging
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# parameters
# ============================================================

TURNOVER_CLIP_MAX = 1e15


# ============================================================
# helpers
# ============================================================

def _safe_numeric(s: pd.Series) -> pd.Series:
    return (
        pd.to_numeric(s, errors="coerce")
        .replace([np.inf, -np.inf], np.nan)
        .fillna(0)
    )


def _resolve_price(df: pd.DataFrame) -> pd.Series:
    """
    price列の優先順位:
    close > price > CurrentPrice
    """

    for col in ["close", "price", "CurrentPrice"]:
        if col in df.columns:
            return _safe_numeric(df[col])

    return pd.Series(0, index=df.index)


def _resolve_volume(df: pd.DataFrame) -> pd.Series:
    if "volume" in df.columns:
        return _safe_numeric(df["volume"])

    return pd.Series(0, index=df.index)


def _sanitize_turnover(s: pd.Series) -> pd.Series:

    s = _safe_numeric(s)

    # negative防止
    s = s.clip(lower=0)

    # 異常値クリップ
    s = s.clip(upper=TURNOVER_CLIP_MAX)

    return s


# ============================================================
# main
# ============================================================

def ensure_turnover(
    df: pd.DataFrame,
    *,
    overwrite: bool = False
) -> pd.DataFrame:
    """
    turnover列を保証生成

    Parameters
    ----------
    overwrite : bool
        Trueなら既存turnoverを再計算
    """

    if df is None or df.empty:
        return df

    try:

        df = df.copy()

        # ----------------------------------------------------
        # 既存尊重
        # ----------------------------------------------------
        if "turnover" in df.columns and not overwrite:

            df["turnover"] = _sanitize_turnover(df["turnover"])
            return df

        # ----------------------------------------------------
        # 計算
        # ----------------------------------------------------
        price = _resolve_price(df)
        volume = _resolve_volume(df)

        turnover = price * volume

        turnover = _sanitize_turnover(turnover)

        df["turnover"] = turnover

        return df

    except Exception:

        logger.exception("[turnover] ensure failed")

        df["turnover"] = 0
        return df


# ============================================================
# liquidity補助（軽量評価）
# ============================================================

def compute_turnover_rank(df: pd.DataFrame) -> pd.Series:
    """
    売買代金ランキング（0〜1）
    """

    if df is None or df.empty or "turnover" not in df.columns:
        return pd.Series(0, index=df.index)

    try:

        rank = df["turnover"].rank(pct=True)

        return _sanitize_turnover(rank)

    except Exception:
        return pd.Series(0, index=df.index)


# ============================================================
# 最新値取得（ユーティリティ）
# ============================================================

def latest_turnover(df: pd.DataFrame):

    if df is None or df.empty:
        return 0

    if "turnover" not in df.columns:
        return 0

    try:
        return float(df["turnover"].iloc[-1])
    except Exception:
        return 0