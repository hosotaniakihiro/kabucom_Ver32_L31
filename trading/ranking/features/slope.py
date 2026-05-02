# ============================================================
# File   : trading/ranking/features/slope.py
# Version: Ver3-PRODUCTION-ULTRA-STABLE-SLOPE
# ------------------------------------------------------------
# ✔ MAベース傾き（ma25 / 任意MA）
# ✔ close直接傾き fallback
# ✔ groupby安全処理
# ✔ datetime順序保証
# ✔ NaN / inf 完全防御
# ✔ dtype崩壊耐性
# ✔ 微分ノイズ抑制（diff + smoothing）
# ✔ スコア化補助（sign / normalized）
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

DEFAULT_MA_COL = "ma25"
ROLL_SMOOTH = 3          # slope平滑化
CLIP_MIN = -1e6
CLIP_MAX = 1e6


# ============================================================
# helpers
# ============================================================

def _safe_col(df: pd.DataFrame, col: str):
    if col in df.columns:
        return df[col]
    return pd.Series(0, index=df.index)


def _sanitize(s: pd.Series) -> pd.Series:
    return (
        s.replace([np.inf, -np.inf], np.nan)
         .fillna(0)
    )


# ============================================================
# slope core
# ============================================================

def _compute_slope(
    df: pd.DataFrame,
    base_col: str
) -> pd.Series:
    """
    指定列の傾き（symbol単位）
    """

    series = _safe_col(df, base_col).astype(float)

    if "symbol" not in df.columns:
        return pd.Series(0, index=df.index)

    try:

        # groupごとに差分
        slope = (
            series.groupby(df["symbol"])
            .diff()
        )

        slope = _sanitize(slope)

        # ノイズ抑制（移動平均）
        slope = (
            slope.groupby(df["symbol"])
            .rolling(ROLL_SMOOTH)
            .mean()
            .reset_index(level=0, drop=True)
        )

        slope = _sanitize(slope)

        return slope

    except Exception:
        logger.exception("[slope] compute failed")
        return pd.Series(0, index=df.index)


# ============================================================
# normalize slope（強さを揃える）
# ============================================================

def _normalize_slope(slope: pd.Series) -> pd.Series:
    try:

        # 絶対値最大で正規化
        max_abs = slope.abs().max()

        if max_abs > 0:
            slope = slope / max_abs

        return slope

    except Exception:
        return slope


# ============================================================
# public API
# ============================================================

def ensure_slope(
    df: pd.DataFrame,
    *,
    base_col: str = DEFAULT_MA_COL,
    normalize: bool = False
) -> pd.DataFrame:
    """
    slope列を保証生成

    出力:
        df["score_slope"]

    Parameters
    ----------
    base_col : str
        傾きを取る列（通常 ma25）
    normalize : bool
        Trueで-1〜1に正規化
    """

    if df is None or df.empty:
        return df

    try:

        df = df.copy()

        # ----------------------------------------------------
        # 既に存在する場合はそのまま使う
        # ----------------------------------------------------
        if "score_slope" in df.columns:
            return df

        # ----------------------------------------------------
        # datetime順序保証（重要）
        # ----------------------------------------------------
        if "datetime" in df.columns and "symbol" in df.columns:
            df = df.sort_values(["symbol", "datetime"])

        # ----------------------------------------------------
        # MAベース
        # ----------------------------------------------------
        if base_col in df.columns:

            slope = _compute_slope(df, base_col)

        else:
            # fallback（closeベース）
            slope = _compute_slope(df, "close")

        slope = _sanitize(slope)

        # ----------------------------------------------------
        # 正規化（任意）
        # ----------------------------------------------------
        if normalize:
            slope = _normalize_slope(slope)

        # ----------------------------------------------------
        # clip（暴走防止）
        # ----------------------------------------------------
        slope = slope.clip(CLIP_MIN, CLIP_MAX)

        df["score_slope"] = slope

        return df

    except Exception:

        logger.exception("[slope] ensure failed")

        df["score_slope"] = 0
        return df


# ============================================================
# sign only（軽量版）
# ============================================================

def slope_sign(df: pd.DataFrame) -> pd.Series:
    """
    傾きの方向だけ取得（高速）
    """

    if df is None or df.empty:
        return pd.Series(dtype=float)

    slope = _compute_slope(df, DEFAULT_MA_COL)

    return np.sign(_sanitize(slope))


# ============================================================
# utility（単体取得）
# ============================================================

def latest_slope(df: pd.DataFrame):

    if df is None or df.empty:
        return 0

    if "score_slope" not in df.columns:
        return 0

    try:
        return df["score_slope"].iloc[-1]
    except Exception:
        return 0