# ============================================================
# File   : utils/pandas_guard.py
# Version: Ver3.0-ULTRA-STABLE-PANDAS-GUARD
# ------------------------------------------------------------
# ✔ safe concat
# ✔ safe merge
# ✔ safe groupby
# ✔ safe sort
# ✔ safe column access
# ✔ pandas alignment crash防止
# ============================================================

from __future__ import annotations

import logging
import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# safe concat
# ============================================================

def safe_concat(dfs):

    try:

        dfs = [x for x in dfs if x is not None and not x.empty]

        if not dfs:
            return pd.DataFrame()

        return pd.concat(dfs, ignore_index=True)

    except Exception:

        logger.exception("[PANDAS GUARD] concat failed")

        return pd.DataFrame()


# ============================================================
# safe merge
# ============================================================

def safe_merge(left, right, **kwargs):

    try:

        if left is None or right is None:
            return left

        return pd.merge(left, right, **kwargs)

    except Exception:

        logger.exception("[PANDAS GUARD] merge failed")

        return left


# ============================================================
# safe sort
# ============================================================

def safe_sort(df, cols):

    try:

        return df.sort_values(cols, kind="mergesort")

    except Exception:

        logger.warning("[PANDAS GUARD] sort failed")

        return df


# ============================================================
# safe column
# ============================================================

def safe_column(df, col, default=0):

    if col not in df.columns:

        df[col] = default

    return df[col]


# ============================================================
# safe groupby
# ============================================================

def safe_groupby(df, key):

    try:

        return df.groupby(key)

    except Exception:

        logger.warning("[PANDAS GUARD] groupby failed")

        return None