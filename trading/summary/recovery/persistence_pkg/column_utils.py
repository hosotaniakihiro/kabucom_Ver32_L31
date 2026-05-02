# ============================================================
# File   : trading/summary/recovery/persistence_pkg/column_utils.py
# Ver    : PRODUCTION-STABLE-REV9.0-COLUMN-UTILS
# ------------------------------------------------------------
# 【概要】
#   DataFrame basic / duplicate / alias column helpers
# ============================================================

from __future__ import annotations

import logging
from typing import Any, Optional

import pandas as pd

logger = logging.getLogger(__name__)


def safe_df(df: Any) -> pd.DataFrame:
    try:
        if isinstance(df, pd.DataFrame):
            return df.copy()
        return pd.DataFrame()
    except Exception:
        logger.exception("[summary.recovery.persistence] safe_df failed")
        return pd.DataFrame()


def normalize_symbol_value(v: Any) -> str:
    try:
        if pd.isna(v):
            return ""
        s = str(v).strip()
        if not s:
            return ""
        if s.endswith(".0"):
            s2 = s[:-2]
            if s2.isdigit():
                return s2
        return s
    except Exception:
        return ""


def pick_first_existing(df: pd.DataFrame, candidates: list[str]) -> Optional[str]:
    cols = set(df.columns)
    for c in candidates:
        if c in cols:
            return c
    return None


def pick_numeric_series(df: pd.DataFrame, candidates: list[str], default=0.0) -> pd.Series:
    col = pick_first_existing(df, candidates)
    if col is None:
        return pd.Series(default, index=df.index, dtype="float64")
    try:
        s = pd.to_numeric(df[col], errors="coerce")
        s = s.replace([float("inf"), float("-inf")], default)
        return s.fillna(default)
    except Exception:
        return pd.Series(default, index=df.index, dtype="float64")


def pick_numeric_series_nan(df: pd.DataFrame, candidates: list[str]) -> pd.Series:
    col = pick_first_existing(df, candidates)
    if col is None:
        return pd.Series(float("nan"), index=df.index, dtype="float64")
    try:
        s = pd.to_numeric(df[col], errors="coerce")
        s = s.replace([float("inf"), float("-inf")], float("nan"))
        return s
    except Exception:
        return pd.Series(float("nan"), index=df.index, dtype="float64")


def pick_text_series(df: pd.DataFrame, candidates: list[str], default="") -> pd.Series:
    col = pick_first_existing(df, candidates)
    if col is None:
        return pd.Series(default, index=df.index, dtype="object")
    try:
        return df[col].fillna(default).astype(str)
    except Exception:
        return pd.Series(default, index=df.index, dtype="object")


def coalesce_duplicate_columns(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame() if df is None else df

    try:
        if len(df.columns) == len(set(df.columns)):
            return df.copy()
    except Exception:
        return df.copy()

    out = {}
    try:
        for c in list(dict.fromkeys(df.columns)):
            idxs = [i for i, x in enumerate(df.columns) if x == c]
            s = df.iloc[:, idxs[0]]
            for j in idxs[1:]:
                try:
                    s = s.combine_first(df.iloc[:, j])
                except Exception:
                    try:
                        s = s.where(s.notna(), df.iloc[:, j])
                    except Exception:
                        pass
            out[c] = s
        return pd.DataFrame(out).reset_index(drop=True)
    except Exception:
        logger.exception("[summary.recovery.persistence] coalesce_duplicate_columns failed")
        try:
            return df.loc[:, ~df.columns.duplicated()].copy()
        except Exception:
            return df.copy()


def coalesce_into_column(
    df: pd.DataFrame,
    *,
    dst: str,
    sources: list[str],
    numeric: bool = False,
) -> pd.DataFrame:
    out = df.copy()

    try:
        for src in sources:
            if src not in out.columns:
                continue

            src_s = out[src]
            if numeric:
                src_s = pd.to_numeric(src_s, errors="coerce")

            if dst not in out.columns:
                out[dst] = src_s
            else:
                dst_s = out[dst]
                if numeric:
                    dst_s = pd.to_numeric(dst_s, errors="coerce")
                out[dst] = dst_s.where(dst_s.notna(), src_s)
    except Exception:
        logger.exception(
            "[summary.recovery.persistence] coalesce_into_column failed dst=%s sources=%s",
            dst,
            sources,
        )

    return out


__all__ = [
    "safe_df",
    "normalize_symbol_value",
    "pick_first_existing",
    "pick_numeric_series",
    "pick_numeric_series_nan",
    "pick_text_series",
    "coalesce_duplicate_columns",
    "coalesce_into_column",
]