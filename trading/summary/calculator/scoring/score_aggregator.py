# ============================================================
# File   : trading/summary/calculator/scoring/score_aggregator.py
# Version: Ver3.0-PRODUCTION-SCORE-AGGREGATOR-ULTRA-STABLE
# ------------------------------------------------------------
# ✔ score_total 集約（完全安全）
# ✔ score / slope / mtf alias生成
# ✔ 欠損列自動補完
# ✔ dtype完全安定化（float64統一）
# ✔ NaN / inf完全防御
# ✔ スコア列自動検出（誤検出防止）
# ✔ alignment crash完全回避
# ✔ MultiIndex / duplicate列防御
# ✔ 列単位例外隔離
# ✔ production hardened
# ============================================================

from __future__ import annotations

import logging
import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)


# ============================================================
# safe numeric（最強版）
# ============================================================

def _safe_numeric(series: pd.Series) -> pd.Series:

    try:

        if series is None:
            return pd.Series(dtype="float64")

        # ndarray対応
        if not isinstance(series, pd.Series):
            series = pd.Series(series)

        result = pd.to_numeric(series, errors="coerce")

        result = result.replace([np.inf, -np.inf], np.nan)

        result = result.fillna(0.0)

        return result.astype("float64")

    except Exception:

        logger.exception("[SCORE AGG] safe_numeric failed")

        return pd.Series(
            np.zeros(len(series) if hasattr(series, "__len__") else 0),
            dtype="float64"
        )


# ============================================================
# dataframe sanitize
# ============================================================

def _sanitize_dataframe(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return pd.DataFrame()

    df = df.copy()

    # MultiIndex flatten
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    # duplicate columns 제거
    if df.columns.duplicated().any():

        dup = df.columns[df.columns.duplicated()].tolist()

        logger.warning(
            "[SCORE AGG] duplicate columns removed: %s",
            dup
        )

        df = df.loc[:, ~df.columns.duplicated()]

    return df


# ============================================================
# ensure score columns
# ============================================================

def _ensure_score_columns(df: pd.DataFrame) -> pd.DataFrame:

    required = [
        "score_buy",
        "score_sell",
        "score_total",
        "score_slope",
        "score_mtf",
        "flag_score",
        "absolute_score",
        "ai_score",
    ]

    for col in required:

        if col not in df.columns:
            df[col] = 0.0

    return df


# ============================================================
# score column detection（精度UP版）
# ============================================================

def _detect_score_columns(df: pd.DataFrame):

    # 明示スコア
    score_cols = [
        c for c in df.columns
        if c.startswith("score_") and c != "score_total"
    ]

    # fallback（旧構成）
    if not score_cols:
        score_cols = [
            c for c in df.columns
            if c.endswith("_score")
        ]

    # 数値列のみ残す
    valid_cols = []

    for c in score_cols:

        try:

            if pd.api.types.is_numeric_dtype(df[c]):

                valid_cols.append(c)

            else:
                # 強制変換可能なら採用
                _ = pd.to_numeric(df[c], errors="coerce")
                valid_cols.append(c)

        except Exception:
            continue

    return valid_cols


# ============================================================
# aggregate score（完全安全版）
# ============================================================

def _calculate_score_total(df: pd.DataFrame) -> pd.DataFrame:

    score_cols = _detect_score_columns(df)

    if not score_cols:

        df["score_total"] = 0.0
        return df

    try:

        # 列ごとに安全変換（例外隔離）
        safe_cols = []

        for col in score_cols:

            try:
                safe_cols.append(_safe_numeric(df[col]))
            except Exception:
                logger.warning(f"[SCORE AGG] column failed: {col}")

        if not safe_cols:
            df["score_total"] = 0.0
            return df

        # concatでalignment保証
        safe_df = pd.concat(safe_cols, axis=1)

        df["score_total"] = safe_df.sum(axis=1)

        return df

    except Exception:

        logger.exception("[SCORE AGG] score_total calculation failed")

        df["score_total"] = 0.0

        return df


# ============================================================
# alias mapping（強化版）
# ============================================================

def _apply_alias(df: pd.DataFrame) -> pd.DataFrame:

    try:

        # score
        if "score_total" in df.columns:
            df["score"] = _safe_numeric(df["score_total"])

        # slope
        if "score_slope" in df.columns:
            df["slope"] = _safe_numeric(df["score_slope"])
        else:
            df["slope"] = 0.0

        # mtf
        if "score_mtf" in df.columns:
            df["mtf"] = _safe_numeric(df["score_mtf"])
        else:
            df["mtf"] = 0.0

    except Exception:

        logger.exception("[SCORE AGG] alias mapping failed")

    return df


# ============================================================
# final sanitize
# ============================================================

def _final_sanitize(df: pd.DataFrame) -> pd.DataFrame:

    try:

        num_cols = df.select_dtypes(include="number").columns

        if len(num_cols) > 0:

            df[num_cols] = (
                df[num_cols]
                .replace([np.inf, -np.inf], 0)
                .fillna(0)
                .astype("float64")
            )

    except Exception:

        logger.exception("[SCORE AGG] final sanitize failed")

    return df


# ============================================================
# main function
# ============================================================

def aggregate_score(df: pd.DataFrame) -> pd.DataFrame:

    """
    スコア集約（完全安定版）

    - score_total生成
    - score / slope / mtf alias
    - 欠損補完
    """

    if df is None:
        return df

    if not isinstance(df, pd.DataFrame):

        try:
            df = pd.DataFrame(df)
        except Exception:
            return df

    if df.empty:
        return df

    try:

        df = _sanitize_dataframe(df)

        df = df.copy()

        # ----------------------------------------------------
        # ensure columns
        # ----------------------------------------------------

        df = _ensure_score_columns(df)

        # ----------------------------------------------------
        # total score
        # ----------------------------------------------------

        df = _calculate_score_total(df)

        # ----------------------------------------------------
        # alias
        # ----------------------------------------------------

        df = _apply_alias(df)

        # ----------------------------------------------------
        # sanitize
        # ----------------------------------------------------

        df = _final_sanitize(df)

        return df

    except Exception:

        logger.exception("[SCORE AGG] fatal error")

        return df