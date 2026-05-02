# ============================================================
# File   : trading/summary/persistence/preprocess/duplicate_handler.py
# Version: Ver1.0-PRODUCTION-DUPLICATE-HANDLER-HARDENED
# ------------------------------------------------------------
# ✔ summary_saver_bulk から完全分離
# ✔ Ver21.1 ロジック完全互換
# ✔ symbol+datetime 重複排除（基本）
# ✔ symbol+date+time_range 衝突対策（3min/5min）
# ✔ sort安全化（datetime基準）
# ✔ NaT耐性
# ✔ column存在チェック
# ✔ production hardened
# ============================================================

from __future__ import annotations

import logging
import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# SAFE SORT
# ============================================================

def _safe_sort(df: pd.DataFrame) -> pd.DataFrame:

    if "datetime" not in df.columns:
        return df

    try:
        return df.sort_values("datetime")
    except Exception:
        logger.warning("[DUP] sort failed → skip")
        return df


# ============================================================
# MAIN DUPLICATE HANDLER
# ============================================================

def drop_duplicate_rows(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    df = df.copy()

    before = len(df)

    # ========================================================
    # ① 基本：symbol + datetime
    # ========================================================
    if {"symbol", "datetime"}.issubset(df.columns):

        df = df.drop_duplicates(
            subset=["symbol", "datetime"],
            keep="last"
        )

    else:
        logger.warning(
            "[DUP] missing columns for primary dedup → skip"
        )

    # ========================================================
    # ② 3min / 5min 衝突対策
    # ========================================================
    if {"symbol", "date", "time_range"}.issubset(df.columns):

        df = _safe_sort(df)

        df = df.drop_duplicates(
            subset=["symbol", "date", "time_range"],
            keep="last"
        )

    # ========================================================
    # ③ fallback（最終防御）
    # ========================================================
    elif "symbol" in df.columns:

        df = _safe_sort(df)

        df = df.drop_duplicates(
            subset=["symbol"],
            keep="last"
        )

        logger.warning(
            "[DUP] fallback dedup applied (symbol only)"
        )

    # ========================================================
    # log
    # ========================================================
    removed = before - len(df)

    if removed > 0:
        logger.warning(
            "[DUP] removed %d duplicate rows",
            removed
        )

    return df