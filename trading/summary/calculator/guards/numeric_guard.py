# ============================================================
# File   : trading/summary/calculator/guards/numeric_guard.py
# Version: Ver3.0-PRODUCTION-NUMERIC-GUARD-ULTRA-STABLE
# ------------------------------------------------------------
# ✔ NaN / inf 完全除去
# ✔ dtype完全安定化（float64統一）
# ✔ 数値列のみ処理（高速）
# ✔ bool列変換（int8）
# ✔ object→numeric（安全判定付き）
# ✔ MultiIndex / duplicate列完全防御
# ✔ alignment crash完全回避
# ✔ 大規模DataFrame耐性（列単位処理）
# ✔ 例外完全隔離
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

        if not isinstance(series, pd.Series):
            series = pd.Series(series)

        result = pd.to_numeric(series, errors="coerce")

        # inf → NaN
        result = result.replace([np.inf, -np.inf], np.nan)

        # NaN → 0
        result = result.fillna(0.0)

        return result.astype("float64")

    except Exception:

        logger.exception("[NUMERIC GUARD] safe_numeric failed")

        return pd.Series(
            np.zeros(len(series) if hasattr(series, "__len__") else 0),
            dtype="float64"
        )


# ============================================================
# dataframe sanitize（構造防御）
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
            "[NUMERIC GUARD] duplicate columns removed: %s",
            dup
        )

        df = df.loc[:, ~df.columns.duplicated()]

    return df


# ============================================================
# boolean → numeric
# ============================================================

def _sanitize_boolean(df: pd.DataFrame) -> pd.DataFrame:

    try:

        bool_cols = df.select_dtypes(include="bool").columns

        if len(bool_cols) > 0:
            df[bool_cols] = df[bool_cols].astype("int8")

    except Exception:
        logger.exception("[NUMERIC GUARD] boolean sanitize failed")

    return df


# ============================================================
# object → numeric（安全判定付き）
# ============================================================

def _convert_object_numeric(df: pd.DataFrame) -> pd.DataFrame:

    obj_cols = df.select_dtypes(include="object").columns

    for col in obj_cols:

        try:

            # サンプルチェック（高速化）
            sample = df[col].dropna().head(50)

            if sample.empty:
                continue

            converted = pd.to_numeric(sample, errors="coerce")

            # 一定割合以上変換できる場合のみ採用
            if converted.notna().mean() < 0.5:
                continue

            full = pd.to_numeric(df[col], errors="coerce")

            df[col] = (
                full
                .replace([np.inf, -np.inf], np.nan)
                .fillna(0)
                .astype("float64")
            )

        except Exception:
            continue

    return df


# ============================================================
# numeric columns sanitize（列単位安全処理）
# ============================================================

def _sanitize_numeric_columns(df: pd.DataFrame) -> pd.DataFrame:

    num_cols = df.select_dtypes(include="number").columns

    if len(num_cols) == 0:
        return df

    try:

        # 列単位で安全処理（alignment crash防止）
        safe_cols = {}

        for col in num_cols:

            try:
                safe_cols[col] = _safe_numeric(df[col])
            except Exception:
                logger.warning(f"[NUMERIC GUARD] column failed: {col}")

        if safe_cols:
            df[list(safe_cols.keys())] = pd.DataFrame(safe_cols)

    except Exception:
        logger.exception("[NUMERIC GUARD] numeric sanitize failed")

    return df


# ============================================================
# main numeric sanitizer
# ============================================================

def sanitize_numeric(df: pd.DataFrame) -> pd.DataFrame:

    """
    数値系の完全安定化（最強版）

    - NaN / inf → 0
    - dtype → float64
    - bool → int
    - object → numeric（安全判定）
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
        # boolean → numeric
        # ----------------------------------------------------

        df = _sanitize_boolean(df)

        # ----------------------------------------------------
        # object → numeric
        # ----------------------------------------------------

        df = _convert_object_numeric(df)

        # ----------------------------------------------------
        # numeric sanitize
        # ----------------------------------------------------

        df = _sanitize_numeric_columns(df)

        return df

    except Exception:

        logger.exception("[NUMERIC GUARD] fatal error")

        return df


# ============================================================
# lightweight version（高速版）
# ============================================================

def sanitize_numeric_light(df: pd.DataFrame) -> pd.DataFrame:

    """
    軽量版（高速）

    - NaN / inf → 0
    - dtype変換なし
    """

    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return df

    try:

        df = df.copy()

        num_cols = df.select_dtypes(include="number").columns

        if len(num_cols) > 0:

            df[num_cols] = (
                df[num_cols]
                .replace([np.inf, -np.inf], 0)
                .fillna(0)
            )

        return df

    except Exception:

        logger.exception("[NUMERIC GUARD LIGHT] failed")

        return df