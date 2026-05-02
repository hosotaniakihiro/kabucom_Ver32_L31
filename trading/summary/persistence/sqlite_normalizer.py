# ============================================================
# File   : trading/summary/persistence/sqlite_normalizer.py
# Version: Ver1.0-PRODUCTION-SQLITE-NORMALIZER
# ------------------------------------------------------------
# ✔ SQLite保存前のDataFrame正規化
# ✔ NaN / inf → None
# ✔ pandas.NaT crash防止
# ✔ Timestamp → string
# ✔ datetime → string
# ✔ numpy scalar → python scalar
# ✔ tuple/list/ndarray防御
# ✔ DataFrame列防御
# ✔ dtype stabilization
# ✔ SQLite互換型保証
# ✔ vectorized safe normalize
# ============================================================

from __future__ import annotations

import logging
import numpy as np
import pandas as pd
import datetime as dt

logger = logging.getLogger(__name__)


# ============================================================
# scalar normalize
# ============================================================

def normalize_scalar(value):
    """
    SQLite用スカラー値正規化
    """

    if value is None:
        return None

    # pandas NaT
    if value is pd.NaT:
        return None

    # numpy NaN
    if isinstance(value, float) and np.isnan(value):
        return None

    # numpy integer
    if isinstance(value, np.integer):
        return int(value)

    # numpy float
    if isinstance(value, np.floating):
        return float(value)

    # pandas Timestamp
    if isinstance(value, pd.Timestamp):

        if pd.isna(value):
            return None

        return value.strftime("%Y-%m-%d %H:%M:%S")

    # datetime
    if isinstance(value, dt.datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")

    # date
    if isinstance(value, dt.date):
        return value.isoformat()

    # time
    if isinstance(value, dt.time):
        return value.isoformat()

    return value


# ============================================================
# column normalize
# ============================================================

def normalize_column(series: pd.Series) -> pd.Series:
    """
    Series単位のSQLite正規化
    """

    if series is None:
        return series

    try:

        # datetime
        if pd.api.types.is_datetime64_any_dtype(series):

            return series.dt.strftime("%Y-%m-%d %H:%M:%S")

        # numeric
        if pd.api.types.is_numeric_dtype(series):

            s = series.replace([np.inf, -np.inf], np.nan)

            return s.where(pd.notnull(s), None)

        # object
        return series.apply(normalize_scalar)

    except Exception:

        logger.exception("[SQLITE NORMALIZER] column normalize failed")

        return series


# ============================================================
# dataframe normalize
# ============================================================

def normalize_dataframe_for_sqlite(df: pd.DataFrame) -> pd.DataFrame:
    """
    SQLite保存用 DataFrame 正規化
    """

    if df is None or df.empty:
        return df

    df = df.copy()

    try:

        # NaT → None
        df = df.replace({pd.NaT: None})

        # inf
        df = df.replace([np.inf, -np.inf], None)

        # column normalize
        for col in df.columns:

            series = df[col]

            # DataFrame列防御
            if isinstance(series, pd.DataFrame):

                try:
                    series = series.iloc[:, 0]
                except Exception:
                    series = pd.Series([None] * len(df))

            # ndarray防御
            if isinstance(series, np.ndarray):

                try:
                    series = pd.Series(series, index=df.index)
                except Exception:
                    series = pd.Series([None] * len(df))

            # list / tuple
            if isinstance(series, (list, tuple)):

                try:
                    series = pd.Series(series, index=df.index)
                except Exception:
                    series = pd.Series([None] * len(df))

            df[col] = normalize_column(series)

        # NaN → None
        df = df.where(pd.notnull(df), None)

        return df

    except Exception:

        logger.exception("[SQLITE NORMALIZER] dataframe normalize failed")

        return df


# ============================================================
# fast normalize (optional)
# ============================================================

def fast_sqlite_normalize(df: pd.DataFrame) -> pd.DataFrame:
    """
    高速版SQLite normalize
    applymap を使わない
    """

    if df is None or df.empty:
        return df

    df = df.copy()

    try:

        # NaT
        df = df.replace({pd.NaT: None})

        # inf
        df = df.replace([np.inf, -np.inf], None)

        # datetime
        for col in df.columns:

            s = df[col]

            if pd.api.types.is_datetime64_any_dtype(s):

                df[col] = s.dt.strftime("%Y-%m-%d %H:%M:%S")

        # NaN
        df = df.where(pd.notnull(df), None)

        return df

    except Exception:

        logger.exception("[SQLITE NORMALIZER] fast normalize failed")

        return df