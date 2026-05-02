# ============================================================
# File   : trading/ranking/core/numeric_sanitizer.py
# Version: Ver3-PRODUCTION-ULTRA-STABLE-NUMERIC-SANITIZER
# ------------------------------------------------------------
# ✔ NaN / inf 完全除去
# ✔ dtype強制安定化（object→numeric）
# ✔ 異常値クリップ（暴走防止）
# ✔ score / slope 特別ガード
# ✔ safe fallback
# ✔ pandas alignment crash防止
# ✔ production hardened
# ============================================================

from __future__ import annotations

import logging
import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)


# ============================================================
# public API
# ============================================================

def sanitize_numeric(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return pd.DataFrame()

    if not isinstance(df, pd.DataFrame):
        try:
            df = pd.DataFrame(df)
        except Exception:
            return pd.DataFrame()

    df = df.copy()

    df = _convert_object_to_numeric(df)
    df = _replace_inf_nan(df)
    df = _clip_extreme_values(df)
    df = _protect_critical_columns(df)

    return df


# ============================================================
# object → numeric 変換
# ============================================================

def _convert_object_to_numeric(df: pd.DataFrame) -> pd.DataFrame:

    try:

        for col in df.columns:

            if df[col].dtype == "object":

                converted = pd.to_numeric(
                    df[col],
                    errors="ignore"
                )

                df[col] = converted

    except Exception:
        logger.exception("[numeric] object convert failed")

    return df


# ============================================================
# inf / NaN 除去
# ============================================================

def _replace_inf_nan(df: pd.DataFrame) -> pd.DataFrame:

    try:

        num_cols = df.select_dtypes(include=np.number).columns

        df[num_cols] = (
            df[num_cols]
            .replace([np.inf, -np.inf], np.nan)
            .fillna(0)
        )

    except Exception:
        logger.exception("[numeric] inf/nan replace failed")

    return df


# ============================================================
# 異常値クリップ（暴走防止）
# ============================================================

def _clip_extreme_values(df: pd.DataFrame) -> pd.DataFrame:

    try:

        num_cols = df.select_dtypes(include=np.number).columns

        # 全体クリップ（かなり広め）
        df[num_cols] = df[num_cols].clip(-1e12, 1e12)

    except Exception:
        logger.exception("[numeric] clip failed")

    return df


# ============================================================
# 重要カラム保護（スコア暴走防止）
# ============================================================

def _protect_critical_columns(df: pd.DataFrame) -> pd.DataFrame:

    try:

        # score系（ランキング崩壊防止）
        score_cols = [
            "score",
            "score_buy",
            "score_sell",
            "score_mtf",
            "score_slope",
            "entry_timing_score",
            "ignition_score",
            "smart_money_score",
        ]

        for col in score_cols:

            if col in df.columns:

                df[col] = (
                    df[col]
                    .replace([np.inf, -np.inf], 0)
                    .fillna(0)
                    .clip(-1000, 1000)
                )

        # 価格系（異常値防止）
        price_cols = ["open", "high", "low", "close", "vwap"]

        for col in price_cols:

            if col in df.columns:

                df[col] = df[col].clip(0, 1e8)

        # volume（負値防止）
        if "volume" in df.columns:
            df["volume"] = df["volume"].clip(0, 1e12)

        # turnover（負値防止）
        if "turnover" in df.columns:
            df["turnover"] = df["turnover"].clip(0, 1e15)

    except Exception:
        logger.exception("[numeric] critical protection failed")

    return df