# ============================================================
# File   : trading/ranking/features/fallback_score.py
# Version: Ver3-PRODUCTION-ULTRA-STABLE-FALLBACK-SCORE
# ------------------------------------------------------------
# ✔ momentum（リターン）
# ✔ volume surge（出来高急増）
# ✔ VWAP乖離
# ✔ トレンド傾き（MA slope）
# ✔ groupby安全処理
# ✔ NaN / inf 完全防御
# ✔ dtype崩壊耐性
# ✔ スコア暴走防止（clip）
# ✔ pandas alignment crash防止
# ✔ production hardened
# ============================================================

from __future__ import annotations

import logging
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# parameters（調整可能）
# ============================================================

W_MOMENTUM = 50
W_VOLUME = 20
W_VWAP = 30
W_SLOPE = 10

ROLL_VOL = 5
ROLL_MA = 25

SCORE_CLIP_MIN = -1000
SCORE_CLIP_MAX = 1000


# ============================================================
# helpers
# ============================================================

def _safe_series(df: pd.DataFrame, col: str) -> pd.Series:
    """
    存在しない列は0で埋める安全取得
    """
    if col in df.columns:
        return df[col]
    return pd.Series(0, index=df.index)


def _sanitize_series(s: pd.Series) -> pd.Series:
    """
    inf / NaN 安全化
    """
    return (
        s.replace([np.inf, -np.inf], np.nan)
         .fillna(0)
    )


# ============================================================
# main
# ============================================================

def calculate_fallback_score(
    df: pd.DataFrame
) -> tuple[pd.Series, pd.Series]:
    """
    fallback ranking score を計算

    Returns
    -------
    score : pd.Series
    slope : pd.Series
    """

    if df is None or df.empty:
        return (
            pd.Series(dtype=float),
            pd.Series(dtype=float)
        )

    if "symbol" not in df.columns:
        logger.warning("[fallback_score] symbol missing")
        zeros = pd.Series(0, index=df.index)
        return zeros, zeros

    try:

        df = df.copy()

        # ----------------------------------------------------
        # 必須ソート（超重要）
        # ----------------------------------------------------
        if "datetime" in df.columns:
            df = df.sort_values(["symbol", "datetime"])
        else:
            df = df.sort_values(["symbol"])

        close = _safe_series(df, "close").astype(float)
        volume = _safe_series(df, "volume").astype(float)

        # ----------------------------------------------------
        # momentum（リターン）
        # ----------------------------------------------------
        momentum = (
            close.groupby(df["symbol"])
            .pct_change()
        )

        momentum = _sanitize_series(momentum)

        # ----------------------------------------------------
        # volume surge（出来高倍率）
        # ----------------------------------------------------
        vol_ma = (
            volume.groupby(df["symbol"])
            .rolling(ROLL_VOL)
            .mean()
            .reset_index(level=0, drop=True)
        )

        volume_ratio = volume / vol_ma.replace(0, np.nan)
        volume_ratio = _sanitize_series(volume_ratio)

        # ----------------------------------------------------
        # VWAP乖離
        # ----------------------------------------------------
        if "vwap" in df.columns:

            vwap = df["vwap"].replace(0, np.nan).astype(float)

            vwap_score = (close - vwap) / vwap

        else:

            vwap_score = pd.Series(0, index=df.index)

        vwap_score = _sanitize_series(vwap_score)

        # ----------------------------------------------------
        # MAトレンド（傾き）
        # ----------------------------------------------------
        ma = (
            close.groupby(df["symbol"])
            .rolling(ROLL_MA)
            .mean()
            .reset_index(level=0, drop=True)
        )

        slope = (
            ma.groupby(df["symbol"])
            .diff()
        )

        slope = _sanitize_series(slope)

        # ----------------------------------------------------
        # スコア合成
        # ----------------------------------------------------
        score = (
            momentum * W_MOMENTUM
            + volume_ratio * W_VOLUME
            + vwap_score * W_VWAP
            + slope * W_SLOPE
        )

        score = _sanitize_series(score)

        # ----------------------------------------------------
        # 最終クリップ（暴走防止）
        # ----------------------------------------------------
        score = score.clip(SCORE_CLIP_MIN, SCORE_CLIP_MAX)

        return score, slope

    except Exception:

        logger.exception("[fallback_score] failed")

        zeros = pd.Series(0, index=df.index)

        return zeros, zeros