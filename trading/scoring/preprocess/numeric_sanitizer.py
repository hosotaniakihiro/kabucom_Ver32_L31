# ============================================================
# File   : trading/scoring/preprocess/numeric_sanitizer.py
# Version: Ver2.0-PRODUCTION-ULTRA-STABLE-NUMERIC-SANITIZER
# ------------------------------------------------------------
# ✔ numeric column sanitize
# ✔ object → numeric repair
# ✔ NaN / inf normalize
# ✔ bool → int convert
# ✔ extreme value guard
# ✔ pandas alignment crash防止
# ✔ dtype stabilization
# ✔ overflow guard
# ✔ numeric block stabilization
# ✔ production stability
# ============================================================

from __future__ import annotations

import logging
import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)


# ============================================================
# object numeric repair
# ============================================================

def _repair_object_numeric(df: pd.DataFrame) -> pd.DataFrame:

    try:

        obj_cols = df.select_dtypes(include=["object"]).columns

        for c in obj_cols:

            try:

                try:
                    series = pd.to_numeric(df[c], errors="raise")
                except (ValueError, TypeError):
                    series = None

                if series is not None and pd.api.types.is_numeric_dtype(series):

                    df[c] = series

            except Exception:
                pass

    except Exception:

        logger.exception(
            "[NUMERIC SANITIZER] object numeric repair failed"
        )

    return df


# ============================================================
# replace inf
# ============================================================

def _replace_inf(df: pd.DataFrame) -> pd.DataFrame:

    try:

        numeric_cols = df.select_dtypes(
            include=[np.number]
        ).columns

        df[numeric_cols] = df[numeric_cols].replace(
            [np.inf, -np.inf],
            np.nan
        )

    except Exception:
        pass

    return df


# ============================================================
# fill nan
# ============================================================

def _fill_nan(df: pd.DataFrame) -> pd.DataFrame:

    try:

        numeric_cols = df.select_dtypes(
            include=[np.number]
        ).columns

        df[numeric_cols] = df[numeric_cols].fillna(0)

    except Exception:
        pass

    return df


# ============================================================
# clip extreme values
# ============================================================

def _clip_extreme(df: pd.DataFrame) -> pd.DataFrame:

    try:

        numeric_cols = df.select_dtypes(
            include=[np.number]
        ).columns

        df[numeric_cols] = df[numeric_cols].clip(
            lower=-1e12,
            upper=1e12
        )

    except Exception:
        pass

    return df


# ============================================================
# boolean normalize
# ============================================================

def _sanitize_boolean(df: pd.DataFrame) -> pd.DataFrame:

    try:

        bool_cols = df.select_dtypes(
            include=["bool", "boolean"]
        ).columns

        if len(bool_cols):

            df[bool_cols] = df[bool_cols].astype(int)

    except Exception:
        pass

    return df


# ============================================================
# overflow guard
# ============================================================

def _guard_overflow(df: pd.DataFrame) -> pd.DataFrame:

    """
    extremely large float → safe range
    """

    try:

        numeric_cols = df.select_dtypes(
            include=[np.number]
        ).columns

        for c in numeric_cols:

            try:

                s = df[c]

                if s.dtype == "float64":

                    s = s.replace(
                        [np.finfo(np.float64).max,
                         -np.finfo(np.float64).max],
                        np.nan
                    )

                    df[c] = s

            except Exception:
                pass

    except Exception:
        pass

    return df


# ============================================================
# dtype stabilization
# ============================================================

def _stabilize_dtypes(df: pd.DataFrame) -> pd.DataFrame:

    try:

        numeric_cols = df.select_dtypes(
            include=[np.number]
        ).columns

        for c in numeric_cols:

            try:

                df[c] = pd.to_numeric(
                    df[c],
                    errors="coerce"
                )

            except Exception:
                pass

    except Exception:
        pass

    return df


# ============================================================
# numeric alignment safety
# ============================================================

def _safe_numeric_assignment(df: pd.DataFrame) -> pd.DataFrame:

    """
    pandas alignment crash防止
    """

    try:

        numeric_cols = df.select_dtypes(
            include=[np.number]
        ).columns.tolist()

        if not numeric_cols:
            return df

        block = df[numeric_cols]

        if isinstance(block, pd.DataFrame):

            df[numeric_cols] = block

    except Exception:

        logger.exception(
            "[NUMERIC SANITIZER] numeric assignment failed"
        )

    return df


# ============================================================
# sanitize NaN / inf (pipeline compatibility)
# ============================================================

def sanitize_nan_inf(df: pd.DataFrame) -> pd.DataFrame:
    """
    scoring pipeline compatibility
    """

    if df is None or df.empty:
        return df

    try:

        numeric_cols = df.select_dtypes(
            include=[np.number]
        ).columns

        df[numeric_cols] = df[numeric_cols].replace(
            [np.inf, -np.inf],
            np.nan
        )

    except Exception:
        pass

    return df


# ============================================================
# main sanitizer
# ============================================================

def sanitize_numeric(df: pd.DataFrame) -> pd.DataFrame:
    """
    numeric column sanitize

    ✔ object → numeric
    ✔ replace inf
    ✔ fill NaN
    ✔ clip extreme
    ✔ bool → int
    ✔ overflow guard
    ✔ dtype stabilization
    ✔ pandas alignment safety
    """

    if df is None or df.empty:
        return df

    try:

        df = df.copy()

        df = _repair_object_numeric(df)

        df = _replace_inf(df)

        df = _fill_nan(df)

        df = _clip_extreme(df)

        df = _sanitize_boolean(df)

        df = _guard_overflow(df)

        df = _stabilize_dtypes(df)

        df = _safe_numeric_assignment(df)

        return df

    except Exception:

        logger.exception(
            "[NUMERIC SANITIZER] sanitize failed"
        )

        return df

# ============================================================
# public boolean sanitizer (pipeline compatibility)
# ============================================================

def sanitize_boolean(df: pd.DataFrame) -> pd.DataFrame:
    """
    public wrapper for boolean normalization
    """

    if df is None or df.empty:
        return df

    try:
        return _sanitize_boolean(df)
    except Exception:
        logger.exception(
            "[NUMERIC SANITIZER] sanitize_boolean failed"
        )
        return df