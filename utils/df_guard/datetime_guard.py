# ============================================================
# File   : utils/df_guard/datetime_guard.py
# Version: Ver1.0-INSTITUTIONAL-DATETIME-GUARD
# ------------------------------------------------------------
# ✔ datetime列生成（alias対応）
# ✔ datetime index復元
# ✔ datetime normalize（to_datetime）
# ✔ timezone安全
# ✔ NaT削除
# ✔ pandas崩壊防止
# ✔ production safe
# ============================================================

from __future__ import annotations

import logging
import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# datetime index recovery
# ============================================================

def recover_datetime_from_index(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    try:

        if "datetime" not in df.columns:

            if df.index.name == "datetime":

                df = df.reset_index()

                logger.warning(
                    "[DATETIME GUARD] datetime recovered from index"
                )

    except Exception as e:

        logger.warning(
            "[DATETIME GUARD] index recovery failed: %s", e
        )

    return df


# ============================================================
# datetime fallback repair（alias対応）
# ============================================================

def repair_datetime(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    if "datetime" in df.columns:
        return df

    try:

        aliases = [
            "end_time",
            "start_time",
            "timestamp",
            "time",
            "t_floor",
            "date",
        ]

        for col in aliases:

            if col in df.columns:

                df["datetime"] = df[col]

                logger.warning(
                    "[DATETIME GUARD] alias used: %s -> datetime",
                    col
                )

                return df

        logger.error(
            "[DATETIME GUARD] no datetime column detected"
        )

    except Exception as e:

        logger.warning(
            "[DATETIME GUARD] repair failed: %s", e
        )

    return df


# ============================================================
# datetime normalize（核心）
# ============================================================

def normalize_datetime(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    if "datetime" not in df.columns:
        return df

    try:

        df = df.copy()

        df["datetime"] = pd.to_datetime(
            df["datetime"],
            errors="coerce",
            utc=False
        )

        before = len(df)

        df = df[df["datetime"].notna()]

        dropped = before - len(df)

        if dropped > 0:

            logger.warning(
                "[DATETIME GUARD] dropped invalid datetime rows: %s",
                dropped
            )

    except Exception as e:

        logger.warning(
            "[DATETIME GUARD] normalize failed: %s", e
        )

    return df


# ============================================================
# timezone safety（任意）
# ============================================================

def ensure_timezone_naive(df: pd.DataFrame) -> pd.DataFrame:
    """
    tz付きdatetimeをnaiveに統一（SQLite対策）
    """

    if df is None or df.empty:
        return df

    if "datetime" not in df.columns:
        return df

    try:

        if hasattr(df["datetime"].dtype, "tz") and df["datetime"].dtype.tz is not None:

            df = df.copy()

            df["datetime"] = df["datetime"].dt.tz_localize(None)

            logger.warning(
                "[DATETIME GUARD] timezone removed (naive)"
            )

    except Exception as e:

        logger.warning(
            "[DATETIME GUARD] timezone normalize failed: %s", e
        )

    return df


# ============================================================
# datetime sort（安定化）
# ============================================================

def sort_by_datetime(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    if "datetime" not in df.columns:
        return df

    try:

        return df.sort_values("datetime", kind="mergesort")

    except Exception:
        return df


# ============================================================
# FULL PIPELINE
# ============================================================

def ensure_datetime(df: pd.DataFrame) -> pd.DataFrame:

    if df is None:
        return pd.DataFrame()

    if df.empty:
        return df

    try:

        df = recover_datetime_from_index(df)

        df = repair_datetime(df)

        df = normalize_datetime(df)

        df = ensure_timezone_naive(df)

        df = sort_by_datetime(df)

    except Exception as e:

        logger.exception(
            "[DATETIME GUARD] ensure_datetime failed: %s", e
        )

    return df


# ============================================================
# latest timestamp取得
# ============================================================

def get_latest_timestamp(df: pd.DataFrame):

    if df is None or df.empty:
        return None

    if "datetime" not in df.columns:
        return None

    try:
        return df["datetime"].max()
    except Exception:
        return None


# ============================================================
# datetime差分抽出
# ============================================================

def filter_newer_than(
    df: pd.DataFrame,
    last_dt
) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    if "datetime" not in df.columns:
        return df

    if last_dt is None:
        return df

    try:

        return df[df["datetime"] > last_dt]

    except Exception:
        return df


# ============================================================
# public API
# ============================================================

__all__ = [
    "recover_datetime_from_index",
    "repair_datetime",
    "normalize_datetime",
    "ensure_timezone_naive",
    "sort_by_datetime",
    "ensure_datetime",
    "get_latest_timestamp",
    "filter_newer_than",
]