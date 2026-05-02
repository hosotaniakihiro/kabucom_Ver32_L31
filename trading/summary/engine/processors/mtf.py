# ============================================================
# File   : trading/summary/engine/processors/mtf.py
# Version: Ver31-PRODUCTION-MTF-PROCESSOR-FULL
# ------------------------------------------------------------
# ✔ apply_advanced_mtf 安全ラップ
# ✔ duplicate columns 完全防止
# ✔ enhance_guard統合
# ✔ 空DF安全
# ✔ crash防止
# ✔ fallback（元DF保持）
# ✔ production safe
# ============================================================

from __future__ import annotations

import logging
import pandas as pd

from trading.scoring.advanced_mtf import apply_advanced_mtf

from trading.summary.engine.guards.enhance_guard import (
    enhance_guard,
    drop_duplicate_columns,
)

logger = logging.getLogger(__name__)


# ============================================================
# core mtf
# ============================================================

def apply_mtf(df: pd.DataFrame) -> pd.DataFrame:
    """
    MTF安全適用

    ✔ slope / trend / multi timeframe
    ✔ fallbackあり
    """

    if df is None or df.empty:
        return df

    try:

        # ----------------------------------------------------
        # 0. 事前防御（最重要）
        # ----------------------------------------------------
        df = drop_duplicate_columns(df)

        if "symbol" not in df.columns:
            logger.warning("[MTF] symbol missing")
            return df

        if "datetime" not in df.columns:
            logger.warning("[MTF] datetime missing")
            return df

        # ----------------------------------------------------
        # 1. MTF
        # ----------------------------------------------------
        df_mtf = apply_advanced_mtf(df)

        # fallback
        if df_mtf is None or df_mtf.empty:
            logger.warning("[MTF] empty result → fallback")
            return df

        # ----------------------------------------------------
        # 2. duplicate防御（最重要）
        # ----------------------------------------------------
        df_mtf = drop_duplicate_columns(df_mtf)

        # ----------------------------------------------------
        # 3. guard
        # ----------------------------------------------------
        df_mtf = enhance_guard(df_mtf)

        return df_mtf

    except Exception:

        logger.exception("[MTF PROCESSOR] failed")

        return df


# ============================================================
# strict version（デバッグ用）
# ============================================================

def apply_mtf_strict(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    try:

        before_cols = set(df.columns)

        df_out = apply_mtf(df)

        after_cols = set(df_out.columns)

        added = after_cols - before_cols

        if added:
            logger.info(
                "[MTF PROCESSOR] added columns: %s",
                list(added)[:20]
            )

        return df_out

    except Exception:

        logger.exception("[MTF PROCESSOR STRICT] failed")

        return df


# ============================================================
# safe wrapper（engine互換）
# ============================================================

def safe_mtf(df: pd.DataFrame) -> pd.DataFrame:

    return apply_mtf(df)


# ============================================================
# multi mtf helper（必要なら）
# ============================================================

def apply_multi_mtf(
    df_1m: pd.DataFrame,
    df_3m: pd.DataFrame,
    df_5m: pd.DataFrame,
) -> dict[str, pd.DataFrame]:

    try:

        return {
            "1min": apply_mtf(df_1m),
            "3min": apply_mtf(df_3m),
            "5min": apply_mtf(df_5m),
        }

    except Exception:

        logger.exception("[MTF PROCESSOR] multi failed")

        return {
            "1min": df_1m,
            "3min": df_3m,
            "5min": df_5m,
        }


# ============================================================
# public API
# ============================================================

__all__ = [
    "apply_mtf",
    "apply_mtf_strict",
    "apply_multi_mtf",
    "safe_mtf",
]