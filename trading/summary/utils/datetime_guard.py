# ============================================================
# File   : trading/summary/utils/datetime_guard.py
# Version: Ver1.0-PRODUCTION-DATETIME-GUARD
# ------------------------------------------------------------
# ✔ datetime column guarantee
# ✔ t_floor / start_time / end_time fallback
# ✔ object → datetime safe convert
# ✔ NaT row drop
# ✔ duplicate datetime column guard
# ✔ symbol + datetime sort safety
# ✔ timezone removal
# ✔ pandas alignment crash防止
# ✔ production logging
# ============================================================

from __future__ import annotations

import logging
import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# SAFE DATETIME CONVERT
# ============================================================

def _safe_to_datetime(series: pd.Series) -> pd.Series:

    try:

        s = pd.to_datetime(series, errors="coerce")

        try:
            s = s.dt.tz_localize(None)
        except Exception:
            pass

        return s

    except Exception:

        logger.warning(
            "[DATETIME GUARD] datetime conversion failed"
        )

        return pd.to_datetime(series, errors="coerce")


# ============================================================
# DUPLICATE DATETIME COLUMN GUARD
# ============================================================

def _fix_datetime_duplicate(df: pd.DataFrame) -> pd.DataFrame:

    cols = list(df.columns)

    if cols.count("datetime") <= 1:
        return df

    first = cols.index("datetime")

    drop = [
        i for i, c in enumerate(cols)
        if c == "datetime" and i != first
    ]

    logger.warning(
        "[DATETIME GUARD] duplicate datetime column removed"
    )

    return df.drop(df.columns[drop], axis=1)


# ============================================================
# DETECT TIME COLUMN
# ============================================================

def _detect_time_column(df: pd.DataFrame):

    priority = [
        "datetime",
        "t_floor",
        "end_time",
        "start_time",
        "timestamp",
        "time"
    ]

    for col in priority:

        if col in df.columns:
            return col

    return None


# ============================================================
# ENSURE DATETIME COLUMN
# ============================================================

def ensure_datetime(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return pd.DataFrame()

    df = df.copy()

    df = _fix_datetime_duplicate(df)

    if "datetime" in df.columns:

        df["datetime"] = _safe_to_datetime(df["datetime"])

    else:

        time_col = _detect_time_column(df)

        if time_col is None:

            logger.error(
                "[DATETIME GUARD] no time column detected"
            )

            return df

        try:

            df["datetime"] = _safe_to_datetime(df[time_col])

        except Exception:

            logger.exception(
                "[DATETIME GUARD] datetime creation failed"
            )

            return df

    # remove NaT
    before = len(df)

    df = df[df["datetime"].notna()]

    dropped = before - len(df)

    if dropped > 0:

        logger.warning(
            "[DATETIME GUARD] dropped rows without datetime: %s",
            dropped
        )

    return df


# ============================================================
# SORT SAFETY
# ============================================================

def sort_by_symbol_datetime(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    if "datetime" not in df.columns:
        return df

    try:

        if "symbol" in df.columns:

            df = df.sort_values(
                ["symbol", "datetime"],
                kind="mergesort"
            )

        else:

            df = df.sort_values(
                "datetime",
                kind="mergesort"
            )

    except Exception:

        logger.warning(
            "[DATETIME GUARD] sort failed"
        )

    return df


# ============================================================
# FULL DATETIME SANITIZE
# ============================================================

def sanitize_datetime(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return pd.DataFrame()

    try:

        df = ensure_datetime(df)

        df = sort_by_symbol_datetime(df)

    except Exception:

        logger.exception(
            "[DATETIME GUARD] sanitize failed"
        )

    return df


# ============================================================
# GET LATEST TIME
# ============================================================

def get_latest_datetime(df: pd.DataFrame):

    if df is None or df.empty:
        return None

    if "datetime" not in df.columns:
        return None

    try:
        return df["datetime"].max()
    except Exception:
        return None


# ============================================================
# GET EARLIEST TIME
# ============================================================

def get_earliest_datetime(df: pd.DataFrame):

    if df is None or df.empty:
        return None

    if "datetime" not in df.columns:
        return None

    try:
        return df["datetime"].min()
    except Exception:
        return None
    