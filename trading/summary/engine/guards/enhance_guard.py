# ============================================================
# File   : trading/summary/engine/guards/enhance_guard.py
# Version: Ver31-PRODUCTION-ENHANCE-GUARD-FULL
# ------------------------------------------------------------
# ✔ duplicate columns完全除去（最重要）
# ✔ internal df_guard完全保持
# ✔ utils df_guard統合
# ✔ symbol完全安定化
# ✔ datetime安全化
# ✔ OHLC保証
# ✔ numeric安全化
# ✔ index安全化
# ✔ 非破壊設計
# ✔ production safe
# ============================================================

from __future__ import annotations

import logging
import pandas as pd

# ============================================================
# INTERNAL GUARD（既存）
# ============================================================

from trading.summary.engine.internal.df_guard import (
    sanitize_dataframe,
    safe_datetime,
    drop_duplicate_ohlc,
)

# ============================================================
# NEW GUARD（utils）
# ============================================================

from utils.df_guard.core import sanitize
from utils.df_guard.symbol_guard import ensure_symbol
from utils.df_guard.ohlc_guard import ensure_ohlc
from utils.df_guard.numeric_guard import sanitize_numeric
from utils.df_guard.index_guard import ensure_index

logger = logging.getLogger(__name__)


# ============================================================
# duplicate columns guard（最重要）
# ============================================================

def drop_duplicate_columns(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    try:

        before = len(df.columns)

        df = df.loc[:, ~df.columns.duplicated()]

        after = len(df.columns)

        if before != after:

            logger.warning(
                "[ENHANCE GUARD] duplicate columns removed: %s",
                before - after
            )

    except Exception:

        logger.exception("[ENHANCE GUARD] duplicate column drop failed")

    return df


# ============================================================
# main enhance guard
# ============================================================

def enhance_guard(df: pd.DataFrame) -> pd.DataFrame:
    """
    全ガード統合レイヤー（最重要）

    ✔ ここを通れば壊れない
    ✔ 順序依存を排除
    """

    if df is None or df.empty:
        return df

    try:

        # ----------------------------------------------------
        # 0. duplicate columns（最優先）
        # ----------------------------------------------------
        df = drop_duplicate_columns(df)

        # ----------------------------------------------------
        # 1. internal guard（既存）
        # ----------------------------------------------------
        df = sanitize_dataframe(df)
        df = safe_datetime(df)
        df = drop_duplicate_ohlc(df)

        # ----------------------------------------------------
        # 2. utils guard（追加）
        # ----------------------------------------------------
        df = sanitize(df)

        # symbol（ここで完全修復）
        df = ensure_symbol(df)

        # OHLC保証
        df = ensure_ohlc(df)

        # numeric
        df = sanitize_numeric(df)

        # index
        df = ensure_index(df)

        # ----------------------------------------------------
        # 3. 再チェック（安全化）
        # ----------------------------------------------------
        df = drop_duplicate_columns(df)

        if "datetime" in df.columns:
            df = df[df["datetime"].notna()]

    except Exception:

        logger.exception("[ENHANCE GUARD] enhance failed")

    return df


# ============================================================
# public API
# ============================================================

__all__ = [
    "enhance_guard",
    "drop_duplicate_columns",
]