# ============================================================
# File   : utils/datetime_guard.py
# Version: Ver1.0-PRODUCTION-DATETIME-GUARD
# ------------------------------------------------------------
# ✔ datetime列完全保証
# ✔ DatetimeIndex対応
# ✔ time/date/start_time/end_time吸収
# ✔ tz-aware → tz-naive
# ✔ NaT除去
# ✔ duplicate column guard
# ✔ pandas安全化
# ✔ scheduler安全
# ============================================================

from __future__ import annotations

import logging
import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# duplicate column guard
# ============================================================

def remove_duplicate_columns(df: pd.DataFrame) -> pd.DataFrame:

    try:

        if df is None:
            return pd.DataFrame()

        if not isinstance(df, pd.DataFrame):
            df = pd.DataFrame(df)

        if df.empty:
            return pd.DataFrame()

        return df.loc[:, ~df.columns.duplicated()]

    except Exception:

        logger.exception("[DATETIME GUARD] duplicate column repair failed")

        return pd.DataFrame()


# ============================================================
# datetime detect
# ============================================================

def detect_datetime_column(df: pd.DataFrame) -> str | None:

    try:

        candidates = [
            "datetime",
            "time",
            "date",
            "t_floor",
            "start_time",
            "end_time",
            "timestamp",
            "dt",
        ]

        for c in candidates:

            if c in df.columns:
                return c

        # fallback detection
        for c in df.columns:

            name = str(c).lower()

            if "time" in name or "date" in name or "dt" in name:
                return c

        return None

    except Exception:

        logger.exception("[DATETIME GUARD] detect failed")

        return None


# ============================================================
# tz remove
# ============================================================

def remove_timezone(series):

    try:

        if getattr(series.dt, "tz", None) is not None:
            return series.dt.tz_convert(None)

        return series

    except Exception:

        return series


# ============================================================
# finalize datetime
# ============================================================

def finalize_datetime(df: pd.DataFrame) -> pd.DataFrame:

    try:

        if "datetime" not in df.columns:
            return pd.DataFrame()

        df["datetime"] = pd.to_datetime(
            df["datetime"],
            errors="coerce"
        )

        df = df.dropna(subset=["datetime"])

        if df.empty:
            return pd.DataFrame()

        try:
            df["datetime"] = remove_timezone(df["datetime"])
        except Exception:
            pass

        df = df.sort_values("datetime")

        return df.reset_index(drop=True)

    except Exception:

        logger.exception("[DATETIME GUARD] finalize failed")

        return pd.DataFrame()


# ============================================================
# main datetime guard
# ============================================================

def ensure_datetime(df: pd.DataFrame) -> pd.DataFrame:

    try:

        if df is None:
            return pd.DataFrame()

        if not isinstance(df, pd.DataFrame):
            try:
                df = pd.DataFrame(df)
            except Exception:
                return pd.DataFrame()

        if df.empty:
            return pd.DataFrame()

        df = df.copy()

        # duplicate column guard
        df = remove_duplicate_columns(df)

        # DatetimeIndex
        try:

            if isinstance(df.index, pd.DatetimeIndex):

                df["datetime"] = pd.to_datetime(
                    df.index,
                    errors="coerce"
                )

                df = df.reset_index(drop=True)

                return finalize_datetime(df)

        except Exception:
            pass

        # datetime column detect
        if "datetime" not in df.columns:

            col = detect_datetime_column(df)

            if col is None:

                logger.warning(
                    "[DATETIME GUARD] datetime column not found"
                )

                return pd.DataFrame()

            df["datetime"] = pd.to_datetime(
                df[col],
                errors="coerce"
            )

        return finalize_datetime(df)

    except Exception:

        logger.exception("[DATETIME GUARD] fatal error")

        return pd.DataFrame()


# ============================================================
# helper
# ============================================================

def ensure_datetime_column(df: pd.DataFrame) -> pd.DataFrame:
    """
    ensure_datetime alias
    """

    return ensure_datetime(df)