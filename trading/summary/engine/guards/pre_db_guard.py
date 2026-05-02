# ============================================================
# File   : trading/summary/engine/guards/pre_db_guard.py
# Version: Ver31-PRODUCTION-PRE-DB-GUARD-FULL
# ------------------------------------------------------------
# ✔ enhance_guard統合
# ✔ NULL / NaT完全排除
# ✔ symbol / datetime 必須保証
# ✔ duplicate（symbol + datetime）排除
# ✔ 型安全化
# ✔ DB事故防止
# ✔ 非破壊設計
# ✔ production safe
# ============================================================

from __future__ import annotations

import logging
import pandas as pd

from trading.summary.engine.guards.enhance_guard import enhance_guard

logger = logging.getLogger(__name__)


# ============================================================
# duplicate row guard（DB用）
# ============================================================

def drop_duplicate_for_db(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    try:

        if "symbol" in df.columns and "datetime" in df.columns:

            before = len(df)

            df = (
                df
                .sort_values(["symbol", "datetime"], kind="mergesort")
                .drop_duplicates(
                    subset=["symbol", "datetime"],
                    keep="last"
                )
            )

            after = len(df)

            if before != after:

                logger.warning(
                    "[PRE DB GUARD] duplicate rows removed: %s",
                    before - after
                )

    except Exception:

        logger.exception("[PRE DB GUARD] duplicate row drop failed")

    return df


# ============================================================
# required columns guard
# ============================================================

def ensure_required_columns(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    required = ["symbol", "datetime"]

    missing = [c for c in required if c not in df.columns]

    if missing:

        logger.error(
            "[PRE DB GUARD] missing required columns: %s",
            missing
        )

        return pd.DataFrame()

    return df


# ============================================================
# drop NA rows（最重要）
# ============================================================

def drop_na_for_db(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    try:

        before = len(df)

        df = df.dropna(subset=["symbol", "datetime"])

        after = len(df)

        if before != after:

            logger.warning(
                "[PRE DB GUARD] NA rows removed: %s",
                before - after
            )

    except Exception:

        logger.exception("[PRE DB GUARD] NA drop failed")

    return df


# ============================================================
# dtype safety
# ============================================================

def enforce_dtype(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    try:

        df = df.copy()

        # symbol
        if "symbol" in df.columns:
            df["symbol"] = df["symbol"].astype(str)

        # datetime
        if "datetime" in df.columns:
            df["datetime"] = pd.to_datetime(
                df["datetime"],
                errors="coerce"
            )

    except Exception:

        logger.exception("[PRE DB GUARD] dtype enforce failed")

    return df


# ============================================================
# main guard
# ============================================================

def pre_db_guard(df: pd.DataFrame, interval: int) -> pd.DataFrame:

    if df is None:
        return pd.DataFrame()

    if df.empty:
        return df

    try:

        before = len(df)

        # ----------------------------------------------------
        # 0. ★ 最重要：時間カラム補完
        # ----------------------------------------------------
        df = _ensure_time_columns(df, interval)

        # ----------------------------------------------------
        # 1. 基本整形
        # ----------------------------------------------------
        df = enhance_guard(df)

        # ----------------------------------------------------
        # 2. 必須列チェック
        # ----------------------------------------------------
        df = ensure_required_columns(df)

        if df.empty:
            return df

        # ----------------------------------------------------
        # 3. 型安全化
        # ----------------------------------------------------
        df = enforce_dtype(df)

        # ----------------------------------------------------
        # 4. NA排除
        # ----------------------------------------------------
        df = drop_na_for_db(df)

        if df.empty:
            logger.warning("[PRE DB GUARD] empty after NA drop")
            return df

        # ----------------------------------------------------
        # 5. duplicate排除
        # ----------------------------------------------------
        df = drop_duplicate_for_db(df)

        after = len(df)

        if before != after:
            logger.info("[PRE DB GUARD] rows: %s -> %s", before, after)

    except Exception:
        logger.exception("[PRE DB GUARD] failed")

    return df
# ============================================================
# DATETIME → DATE / TIME_RANGE 補完（NEW）
# ============================================================

def _ensure_time_columns(df: pd.DataFrame, interval: int) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    try:
        df = df.copy()

        dt = pd.to_datetime(df["datetime"], errors="coerce")

        # ----------------------------------------------------
        # date
        # ----------------------------------------------------
        if "date" not in df.columns:
            df["date"] = dt.dt.date
        else:
            df["date"] = df["date"].fillna(dt.dt.date)

        # ----------------------------------------------------
        # time
        # ----------------------------------------------------
        if "time" not in df.columns:
            df["time"] = dt.dt.time

        # ----------------------------------------------------
        # start / end
        # ----------------------------------------------------
        if "start_time" not in df.columns:
            df["start_time"] = dt

        if "end_time" not in df.columns:
            df["end_time"] = dt

        # ----------------------------------------------------
        # time_range
        # ----------------------------------------------------
        if "time_range" not in df.columns:
            df["time_range"] = (
                dt.dt.strftime("%H:%M")
                + " - "
                + (dt + pd.to_timedelta(interval, unit="m")).dt.strftime("%H:%M")
            )
        else:
            mask = df["time_range"].isna()
            df.loc[mask, "time_range"] = (
                dt[mask].dt.strftime("%H:%M")
                + " - "
                + (dt[mask] + pd.to_timedelta(interval, unit="m")).dt.strftime("%H:%M")
            )

        return df

    except Exception:
        logger.exception("time column build failed")
        return df

# ============================================================
# public API
# ============================================================

__all__ = [
    "pre_db_guard",
    "drop_duplicate_for_db",
]