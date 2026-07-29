# ============================================================
# File   : utils/df_guard/numeric_guard.py
# Version: Ver1.0-INSTITUTIONAL-NUMERIC-GUARD
# ------------------------------------------------------------
# ✔ NaN / inf 正規化
# ✔ 数値型強制変換
# ✔ extreme value clip
# ✔ dtype安定化
# ✔ pandas crash防止
# ✔ production safe
# ============================================================

from __future__ import annotations

import logging
import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)


# ============================================================
# replace inf → NaN
# ============================================================

def replace_inf(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    try:

        numeric_cols = df.select_dtypes(include=[np.number]).columns

        for col in numeric_cols:

            if df[col].isin([np.inf, -np.inf]).any():

                df[col] = df[col].replace(
                    [np.inf, -np.inf],
                    np.nan
                )

                logger.warning(
                    "[NUMERIC GUARD] inf replaced in %s",
                    col
                )

    except Exception as e:

        logger.warning(
            "[NUMERIC GUARD] replace_inf failed: %s", e
        )

    return df


# ============================================================
# force numeric（文字列→数値）
# ============================================================

def force_numeric(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    try:

        for col in df.columns:

            if pd.api.types.is_string_dtype(df[col]):

                # 数値に変換できるものだけ変換
                try:
                    converted = pd.to_numeric(df[col], errors="raise")
                except (ValueError, TypeError):
                    converted = None

                if converted is not None and converted.dtype != object:

                    df[col] = converted

    except Exception as e:

        logger.warning(
            "[NUMERIC GUARD] force_numeric failed: %s", e
        )

    return df


# ============================================================
# sanitize numeric（まとめ）
# ============================================================

def sanitize_numeric(df: pd.DataFrame) -> pd.DataFrame:

    if df is None:
        return pd.DataFrame()

    if df.empty:
        return df

    try:

        df = df.copy()

        df = force_numeric(df)

        df = replace_inf(df)

    except Exception as e:

        logger.warning(
            "[NUMERIC GUARD] sanitize failed: %s", e
        )

    return df


# ============================================================
# clip extreme values
# ============================================================

def clip_extreme_values(
    df: pd.DataFrame,
    limit: float = 1e12
) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    try:

        numeric_cols = df.select_dtypes(include=[np.number]).columns

        for col in numeric_cols:

            if df[col].abs().max() > limit:

                df[col] = df[col].clip(-limit, limit)

                logger.warning(
                    "[NUMERIC GUARD] clipped extreme values: %s",
                    col
                )

    except Exception as e:

        logger.warning(
            "[NUMERIC GUARD] clipping failed: %s", e
        )

    return df


# ============================================================
# fill NaN（任意）
# ============================================================

def fill_nan(
    df: pd.DataFrame,
    value=0,
    columns: list[str] | None = None
) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    try:

        if columns is None:

            columns = df.select_dtypes(include=[np.number]).columns

        for col in columns:

            if col in df.columns:

                df[col] = df[col].fillna(value)

    except Exception as e:

        logger.warning(
            "[NUMERIC GUARD] fill_nan failed: %s", e
        )

    return df


# ============================================================
# drop NaN rows（重要カラム）
# ============================================================

def drop_na_rows(
    df: pd.DataFrame,
    required_cols: list[str]
) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    try:

        before = len(df)

        df = df.dropna(subset=required_cols)

        dropped = before - len(df)

        if dropped > 0:

            logger.warning(
                "[NUMERIC GUARD] dropped NaN rows: %s",
                dropped
            )

    except Exception as e:

        logger.warning(
            "[NUMERIC GUARD] drop_na_rows failed: %s", e
        )

    return df


# ============================================================
# safe division（ゼロ除算防止）
# ============================================================

def safe_divide(a, b):

    try:
        return np.divide(
            a,
            b,
            out=np.zeros_like(a, dtype=float),
            where=b != 0
        )
    except Exception:
        return 0


# ============================================================
# public API
# ============================================================

__all__ = [
    "replace_inf",
    "force_numeric",
    "sanitize_numeric",
    "clip_extreme_values",
    "fill_nan",
    "drop_na_rows",
    "safe_divide",
]