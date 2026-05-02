# ==========================================================
# File   : trading/summary/summary_sanitizer.py
# Version: Ver1.0-PRODUCTION-DATAFRAME-SANITIZER
# ----------------------------------------------------------
# ✔ summary_controller runtime安全化を完全分離
# ✔ DataFrame安全化専用モジュール
# ✔ NaN / inf / dtype 防御
# ✔ 必須列保証
# ✔ symbol正規化
# ✔ duplicate column削除
# ✔ 副作用ゼロ
# ✔ 本番運用安定版
# ==========================================================

from __future__ import annotations

import logging
import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)


# ==========================================================
# duplicate column削除
# ==========================================================
def remove_duplicate_columns(df: pd.DataFrame) -> pd.DataFrame:

    try:

        if df is None or df.empty:
            return df

        if df.columns.duplicated().any():

            df = df.loc[:, ~df.columns.duplicated()].copy()

        return df

    except Exception:

        logger.exception("[sanitizer] duplicate column remove failed")
        return df


# ==========================================================
# symbol 正規化
# ==========================================================
def normalize_symbol_column(df: pd.DataFrame) -> pd.DataFrame:

    try:

        if df is None or df.empty:
            return df

        if "symbol" not in df.columns:
            return df

        df = df.copy()

        df["symbol"] = (
            df["symbol"]
            .astype(str)
            .str.strip()
            .str.replace(".0", "", regex=False)
        )

        return df

    except Exception:

        logger.exception("[sanitizer] symbol normalize failed")
        return df


# ==========================================================
# 数値列安全化
# ==========================================================
def ensure_numeric_columns(
        df: pd.DataFrame,
        columns: list[str]
) -> pd.DataFrame:

    try:

        if df is None or df.empty:
            return df

        df = df.copy()

        for col in columns:

            if col not in df.columns:
                df[col] = 0.0

            df[col] = (
                pd.to_numeric(
                    df[col],
                    errors="coerce"
                )
                .replace([np.inf, -np.inf], 0)
                .fillna(0)
            )

        return df

    except Exception:

        logger.exception("[sanitizer] numeric column ensure failed")
        return df


# ==========================================================
# runtime必須列保証
# ==========================================================
def ensure_runtime_columns(df: pd.DataFrame) -> pd.DataFrame:

    try:

        if df is None or df.empty:
            return df

        df = df.copy()

        # ======================================================
        # close保証
        # ======================================================

        if "close" not in df.columns:

            if "close_price" in df.columns:

                df["close"] = df["close_price"]

            else:

                df["close"] = 0.0

        df["close"] = (
            pd.to_numeric(
                df["close"],
                errors="coerce"
            )
            .replace([np.inf, -np.inf], 0)
            .fillna(0)
        )

        # ======================================================
        # turnover保証
        # ======================================================

        if "turnover" not in df.columns:

            if {"volume", "close"}.issubset(df.columns):

                df["turnover"] = (
                    pd.to_numeric(
                        df["volume"],
                        errors="coerce"
                    ).fillna(0)
                    * df["close"]
                )

            else:

                df["turnover"] = 0.0

        # ======================================================
        # ATR互換
        # ======================================================

        if "atr_1m" not in df.columns:

            if "atr" in df.columns:

                df["atr_1m"] = df["atr"]

            else:

                df["atr_1m"] = 0.0

        # ATR列保証

        for col in ["atr_1m", "atr_3m", "atr_5m"]:

            if col not in df.columns:

                df[col] = 0.0

        df = ensure_numeric_columns(
            df,
            ["atr_1m", "atr_3m", "atr_5m"]
        )

        return df

    except Exception:

        logger.exception("[sanitizer] runtime column ensure failed")
        return df


# ==========================================================
# MAIN SANITIZER
# ==========================================================
def sanitize_summary_dataframe(
        df: pd.DataFrame
) -> pd.DataFrame:

    """
    summary dataframe を本番安全状態にする
    """

    try:

        if df is None or df.empty:
            return df

        df = df.copy()

        # duplicate column
        df = remove_duplicate_columns(df)

        # symbol normalize
        df = normalize_symbol_column(df)

        # runtime columns
        df = ensure_runtime_columns(df)

        return df

    except Exception:

        logger.exception("[sanitizer] fatal")

        return df