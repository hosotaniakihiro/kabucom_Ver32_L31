# ============================================================
# File   : utils/time_column_resolver.py
# Version: Ver1.0-TIME-COLUMN-RESOLVER-PRODUCTION
# ------------------------------------------------------------
# ✔ datetime / t_floor / start_time / end_time 自動解決
# ✔ pandas dtype crash 防止
# ✔ object → datetime 安全変換
# ✔ timezone安全除去
# ✔ duplicate column 防止
# ✔ symbol + datetime sort保証
# ✔ pipeline crash防止
# ✔ summary / scoring / yahoo 全互換
# ============================================================

from __future__ import annotations

import logging
import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# datetime safe convert
# ============================================================

def _safe_to_datetime(series: pd.Series) -> pd.Series:
    try:
        s = pd.to_datetime(series, errors="coerce")

        # timezone remove
        if hasattr(s.dt, "tz"):
            s = s.dt.tz_localize(None)

        return s

    except Exception:
        logger.exception("[TIME RESOLVER] datetime conversion failed")
        return pd.to_datetime(series, errors="coerce")


# ============================================================
# detect best time column
# ============================================================

def _detect_time_column(df: pd.DataFrame) -> str | None:

    priority = [
        "datetime",
        "t_floor",
        "start_time",
        "end_time",
        "timestamp",
        "time",
    ]

    for col in priority:
        if col in df.columns:
            return col

    return None


# ============================================================
# resolve time column
# ============================================================

def resolve_time_column(
    df: pd.DataFrame,
    ensure_datetime: bool = True,
    sort: bool = True,
) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    df = df.copy()

    # --------------------------------------------------------
    # duplicate column guard
    # --------------------------------------------------------

    if df.columns.duplicated().any():

        dup = df.columns[df.columns.duplicated()].tolist()

        logger.warning(
            "[TIME RESOLVER] duplicate columns detected -> %s",
            dup,
        )

        df = df.loc[:, ~df.columns.duplicated()].copy()

    # --------------------------------------------------------
    # detect time column
    # --------------------------------------------------------

    time_col = _detect_time_column(df)

    if time_col is None:

        logger.error("[TIME RESOLVER] no time column detected")

        return df

    # --------------------------------------------------------
    # convert to datetime
    # --------------------------------------------------------

    df[time_col] = _safe_to_datetime(df[time_col])

    # --------------------------------------------------------
    # unify datetime column
    # --------------------------------------------------------

    if ensure_datetime:

        if "datetime" not in df.columns:

            df["datetime"] = df[time_col]

        else:

            df["datetime"] = _safe_to_datetime(df["datetime"])

    # --------------------------------------------------------
    # unify t_floor
    # --------------------------------------------------------

    if "t_floor" not in df.columns:

        if "datetime" in df.columns:
            df["t_floor"] = df["datetime"]

    else:

        df["t_floor"] = _safe_to_datetime(df["t_floor"])

    # --------------------------------------------------------
    # start_time
    # --------------------------------------------------------

    if "start_time" in df.columns:
        df["start_time"] = _safe_to_datetime(df["start_time"])

    # --------------------------------------------------------
    # end_time
    # --------------------------------------------------------

    if "end_time" in df.columns:
        df["end_time"] = _safe_to_datetime(df["end_time"])

    # --------------------------------------------------------
    # sorting
    # --------------------------------------------------------

    if sort and "symbol" in df.columns and "datetime" in df.columns:

        try:

            df = df.sort_values(
                ["symbol", "datetime"],
                kind="stable"
            )

        except Exception:
            logger.exception("[TIME RESOLVER] sort failed")

    return df


# ============================================================
# ensure datetime column only
# ============================================================

def ensure_datetime(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    df = resolve_time_column(df)

    if "datetime" not in df.columns:

        logger.error("[TIME RESOLVER] datetime column missing")

    return df


# ============================================================
# get latest timestamp
# ============================================================

def get_latest_time(df: pd.DataFrame):

    if df is None or df.empty:
        return None

    df = resolve_time_column(df)

    if "datetime" not in df.columns:
        return None

    try:
        return df["datetime"].max()

    except Exception:
        logger.exception("[TIME RESOLVER] latest time failed")
        return None


# ============================================================
# get earliest timestamp
# ============================================================

def get_earliest_time(df: pd.DataFrame):

    if df is None or df.empty:
        return None

    df = resolve_time_column(df)

    if "datetime" not in df.columns:
        return None

    try:
        return df["datetime"].min()

    except Exception:
        logger.exception("[TIME RESOLVER] earliest time failed")
        return None