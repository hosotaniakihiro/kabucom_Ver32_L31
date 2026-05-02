# ============================================================
# File   : utils/df_guard/column_guard.py
# Version: Ver1.0-INSTITUTIONAL-COLUMN-GUARD
# ------------------------------------------------------------
# ✔ MultiIndex flatten
# ✔ duplicate column guard
# ✔ datetime duplicate guard
# ✔ column dtype safety
# ✔ pandas alignment crash防止
# ✔ production safe
# ============================================================

from __future__ import annotations

import logging
import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# flatten multiindex columns
# ============================================================

def flatten_columns(df: pd.DataFrame) -> pd.DataFrame:

    if df is None:
        return pd.DataFrame()

    if df.empty:
        return df

    try:

        if isinstance(df.columns, pd.MultiIndex):

            df = df.copy()

            df.columns = [
                "_".join(
                    [str(x) for x in col if x not in (None, "")]
                )
                for col in df.columns.to_flat_index()
            ]

            logger.warning(
                "[COLUMN GUARD] MultiIndex columns flattened"
            )

    except Exception as e:

        logger.warning(
            "[COLUMN GUARD] flatten failed: %s", e
        )

    return df


# ============================================================
# duplicate column guard
# ============================================================

def remove_duplicate_columns(df: pd.DataFrame) -> pd.DataFrame:

    if df is None:
        return pd.DataFrame()

    if df.empty:
        return df

    try:

        if df.columns.duplicated().any():

            dup = df.columns[df.columns.duplicated()].tolist()

            df = df.loc[:, ~df.columns.duplicated()]

            logger.warning(
                "[COLUMN GUARD] duplicate columns removed: %s",
                dup
            )

    except Exception as e:

        logger.warning(
            "[COLUMN GUARD] duplicate removal failed: %s", e
        )

    return df


# ============================================================
# datetime duplicate guard
# ============================================================

def fix_datetime_duplicate(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    try:

        cols = list(df.columns)

        if cols.count("datetime") <= 1:
            return df

        first_idx = cols.index("datetime")

        drop_indices = [
            i for i, c in enumerate(cols)
            if c == "datetime" and i != first_idx
        ]

        if drop_indices:

            df = df.drop(df.columns[drop_indices], axis=1)

            logger.warning(
                "[COLUMN GUARD] duplicated datetime columns removed"
            )

    except Exception as e:

        logger.warning(
            "[COLUMN GUARD] datetime duplicate fix failed: %s", e
        )

    return df


# ============================================================
# normalize column names（任意だが推奨）
# ============================================================

def normalize_column_names(df: pd.DataFrame) -> pd.DataFrame:
    """
    ✔ 全列をstr化
    ✔ 前後空白除去
    ✔ 小文字化（任意）
    """

    if df is None or df.empty:
        return df

    try:

        df = df.copy()

        df.columns = [
            str(c).strip()
            for c in df.columns
        ]

    except Exception as e:

        logger.warning(
            "[COLUMN GUARD] normalize failed: %s", e
        )

    return df


# ============================================================
# ensure required columns（保険）
# ============================================================

def ensure_columns(
    df: pd.DataFrame,
    required_cols: list[str],
    fill_value=None
) -> pd.DataFrame:
    """
    指定カラムが無ければ追加
    """

    if df is None:
        return pd.DataFrame()

    if df.empty:
        return df

    try:

        for col in required_cols:

            if col not in df.columns:
                df[col] = fill_value

                logger.warning(
                    "[COLUMN GUARD] missing column added: %s",
                    col
                )

    except Exception as e:

        logger.warning(
            "[COLUMN GUARD] ensure_columns failed: %s", e
        )

    return df


# ============================================================
# reorder columns（任意）
# ============================================================

def reorder_columns(
    df: pd.DataFrame,
    priority_cols: list[str]
) -> pd.DataFrame:
    """
    指定カラムを前に持ってくる
    """

    if df is None or df.empty:
        return df

    try:

        existing = [c for c in priority_cols if c in df.columns]
        others = [c for c in df.columns if c not in existing]

        return df[existing + others]

    except Exception as e:

        logger.warning(
            "[COLUMN GUARD] reorder failed: %s", e
        )
        return df


# ============================================================
# public API
# ============================================================

__all__ = [
    "flatten_columns",
    "remove_duplicate_columns",
    "fix_datetime_duplicate",
    "normalize_column_names",
    "ensure_columns",
    "reorder_columns",
]