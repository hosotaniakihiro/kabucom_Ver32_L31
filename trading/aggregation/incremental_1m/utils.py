"""
============================================================
utils.py
Incremental1MEngine Utility Functions
------------------------------------------------------------
✔ datetime安全変換
✔ float安全変換
✔ NaN / inf 防御
✔ 異常価格チェック
✔ 異常値チェック
✔ pandas安全処理
✔ 本番安定版
============================================================
"""

from __future__ import annotations

import pandas as pd
import numpy as np
import logging
import datetime as dt

logger = logging.getLogger(__name__)


# ============================================================
# SAFE DATETIME
# ============================================================

def safe_dt(x):
    """
    安全datetime変換
    """

    if x is None:
        return None

    try:

        ts = pd.to_datetime(
            x,
            errors="coerce"
        )

        if pd.isna(ts):
            return None

        if isinstance(ts, pd.Timestamp):

            ts = ts.to_pydatetime()

        return ts.replace(
            tzinfo=None
        )

    except Exception:

        logger.exception(
            "[UTILS] safe_dt failed"
        )

        return None


# ============================================================
# SAFE FLOAT
# ============================================================

def safe_float(v, default=0.0):
    """
    NaN / inf 防御付き float
    """

    try:

        if v is None:
            return float(default)

        val = float(v)

        if np.isnan(val):
            return float(default)

        if np.isinf(val):
            return float(default)

        return val

    except Exception:

        return float(default)


# ============================================================
# SAFE INT
# ============================================================

def safe_int(v, default=0):

    try:

        if v is None:
            return int(default)

        val = int(v)

        return val

    except Exception:

        return int(default)


# ============================================================
# ABNORMAL PRICE
# ============================================================

def is_abnormal_price(price):

    try:

        if price is None:
            return True

        price = float(price)

        if price <= 0:
            return True

        if np.isnan(price):
            return True

        if np.isinf(price):
            return True

        if price > 10_000_000:
            return True

        return False

    except Exception:

        return True


# ============================================================
# ABNORMAL VALUE
# ============================================================

def is_abnormal_value(v):

    try:

        if v is None:
            return True

        if isinstance(v, (int, float)):

            if pd.isna(v):
                return True

            if np.isinf(v):
                return True

            if abs(v) > 1e9:
                return True

        return False

    except Exception:

        return True


# ============================================================
# SAFE DATAFRAME SORT
# ============================================================

def safe_sort_datetime(df):

    try:

        if df is None or df.empty:
            return df

        if "datetime" not in df.columns:
            return df

        df["datetime"] = pd.to_datetime(
            df["datetime"],
            errors="coerce"
        )

        df = df.dropna(
            subset=["datetime"]
        )

        df = df.sort_values(
            "datetime"
        )

        df = df.reset_index(
            drop=True
        )

        return df

    except Exception:

        logger.exception(
            "[UTILS] safe_sort_datetime failed"
        )

        return df


# ============================================================
# SAFE DUPLICATE REMOVE
# ============================================================

def remove_bar_duplicates(df):

    try:

        if df is None or df.empty:
            return df

        if "symbol" not in df.columns:
            return df

        if "datetime" not in df.columns:
            return df

        df = df.drop_duplicates(
            subset=["symbol", "datetime"],
            keep="last"
        )

        return df

    except Exception:

        logger.exception(
            "[UTILS] duplicate remove failed"
        )

        return df


# ============================================================
# SAFE BAR WINDOW
# ============================================================

def limit_bars(df, max_bars=400):

    try:

        if df is None or df.empty:
            return df

        if len(df) <= max_bars:
            return df

        return df.tail(max_bars).reset_index(drop=True)

    except Exception:

        logger.exception(
            "[UTILS] limit bars failed"
        )

        return df


# ============================================================
# CURRENT MINUTE
# ============================================================

def current_minute():

    try:

        now = dt.datetime.now()

        return now.replace(
            second=0,
            microsecond=0
        )

    except Exception:

        return None