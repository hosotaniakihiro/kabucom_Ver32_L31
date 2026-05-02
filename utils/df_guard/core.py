# ============================================================
# File   : utils/df_guard/core.py
# Version: Ver1.1-INSTITUTIONAL-CORE-DATAFRAME-GUARD-FIXED
# ------------------------------------------------------------
# ✔ df_guard統合入口
# ✔ OHLC完全対応（NEW）
# ✔ symbol完全正規化（NEW）
# ✔ 軽量 / フルモード対応
# ✔ 各ガードモジュール統合
# ✔ production safe
# ============================================================

from __future__ import annotations

import logging
import pandas as pd

# ============================================================
# module imports
# ============================================================

from utils.df_guard.column_guard import (
    flatten_columns,
    remove_duplicate_columns,
    fix_datetime_duplicate,
)

from utils.df_guard.ohlc_guard import (
    ensure_ohlc,  # ★ 追加（最重要）
)

from utils.df_guard.datetime_guard import (
    ensure_datetime,
)

from utils.df_guard.numeric_guard import (
    sanitize_numeric,
    clip_extreme_values,
)

from utils.df_guard.index_guard import (
    remove_duplicate_index,
    safe_reset_index,
)

from utils.df_guard.symbol_guard import (
    ensure_symbol,  # ★ 変更（重要）
)

from utils.df_guard.extractor import (
    extract_latest_by_symbol,
)

logger = logging.getLogger(__name__)


# ============================================================
# FULL SANITIZER（重いが完全）
# ============================================================

def sanitize_dataframe(df: pd.DataFrame) -> pd.DataFrame:

    if df is None:
        return pd.DataFrame()

    if not isinstance(df, pd.DataFrame):
        try:
            df = pd.DataFrame(df)
        except Exception:
            logger.warning("[DF GUARD] failed to convert to DataFrame")
            return pd.DataFrame()

    if df.empty:
        return df

    try:

        # --------------------------------------------------------
        # ① column系
        # --------------------------------------------------------
        df = flatten_columns(df)

        # --------------------------------------------------------
        # ② OHLC（最優先）
        # --------------------------------------------------------
        df = ensure_ohlc(df)

        # --------------------------------------------------------
        # ③ column重複系
        # --------------------------------------------------------
        df = fix_datetime_duplicate(df)
        df = remove_duplicate_columns(df)

        # --------------------------------------------------------
        # ④ symbol（超重要）
        # --------------------------------------------------------
        df = ensure_symbol(df)

        # --------------------------------------------------------
        # ⑤ datetime
        # --------------------------------------------------------
        df = ensure_datetime(df)

        # --------------------------------------------------------
        # ⑥ numeric
        # --------------------------------------------------------
        df = sanitize_numeric(df)
        df = clip_extreme_values(df)

        # --------------------------------------------------------
        # ⑦ index
        # --------------------------------------------------------
        df = remove_duplicate_index(df)
        df = safe_reset_index(df)

    except Exception as e:

        logger.exception("[DF GUARD] sanitize failed: %s", e)

    return df


# ============================================================
# LIGHT SANITIZER（高速）
# ============================================================

def sanitize_dataframe_light(df: pd.DataFrame) -> pd.DataFrame:

    if df is None:
        return pd.DataFrame()

    if not isinstance(df, pd.DataFrame):
        try:
            df = pd.DataFrame(df)
        except Exception:
            return pd.DataFrame()

    if df.empty:
        return df

    try:

        # --------------------------------------------------------
        # 最低限（リアルタイム用）
        # --------------------------------------------------------
        df = ensure_symbol(df)
        df = ensure_datetime(df)
        df = fix_datetime_duplicate(df)
        df = remove_duplicate_columns(df)

    except Exception:
        logger.warning("[DF GUARD] light sanitize failed")

    return df


# ============================================================
# SAFE ENTRY（推奨入口）
# ============================================================

def sanitize(df: pd.DataFrame, mode: str = "full") -> pd.DataFrame:

    if mode == "light":
        return sanitize_dataframe_light(df)

    return sanitize_dataframe(df)


# ============================================================
# ensure dataframe
# ============================================================

def ensure_dataframe(df) -> pd.DataFrame:

    if df is None:
        return pd.DataFrame()

    if isinstance(df, pd.DataFrame):
        return df

    try:
        return pd.DataFrame(df)
    except Exception:
        logger.warning("[DF GUARD] ensure_dataframe failed")
        return pd.DataFrame()


# ============================================================
# backward compatibility
# ============================================================

def repair_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    return sanitize_dataframe(df)


def sanitize_datetime(df: pd.DataFrame) -> pd.DataFrame:
    return ensure_datetime(df)


# ============================================================
# re-export（外部から使いやすく）
# ============================================================

__all__ = [
    "sanitize_dataframe",
    "sanitize_dataframe_light",
    "sanitize",
    "ensure_dataframe",
    "repair_dataframe",
    "sanitize_datetime",
    "extract_latest_by_symbol",
]