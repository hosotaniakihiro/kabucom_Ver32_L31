# ============================================================
# File   : trading/summary/pipeline/dataframe_safe.py
# Version: Ver32_L05-SPLIT-DATAFRAME-SAFE
# Purpose:
#   summary_pipeline 用 DataFrame 安全化ユーティリティ
# ============================================================

from __future__ import annotations

import logging
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


def is_scalar_like(v: Any) -> bool:
    return isinstance(
        v,
        (
            str,
            bytes,
            int,
            float,
            bool,
            type(None),
            pd.Timestamp,
            np.generic,
        ),
    )


def safe_to_frame(value: Any, name: str = "df") -> pd.DataFrame:
    try:
        if value is None:
            return pd.DataFrame()

        if isinstance(value, pd.DataFrame):
            return value.copy()

        if isinstance(value, pd.Series):
            return value.to_frame().T.reset_index(drop=True)

        if isinstance(value, dict):
            for key in (
                "summary_latest_df",
                "summary_df",
                "df",
                "data",
                "result_df",
                "merged_df",
                "output_df",
            ):
                obj = value.get(key)
                if isinstance(obj, pd.DataFrame):
                    return obj.copy()
                if isinstance(obj, pd.Series):
                    return obj.to_frame().T.reset_index(drop=True)

            safe_row = {}
            for k, v in value.items():
                if is_scalar_like(v):
                    safe_row[str(k)] = v
            return pd.DataFrame([safe_row]) if safe_row else pd.DataFrame()

        if isinstance(value, (list, tuple)):
            if len(value) == 0:
                return pd.DataFrame()

            if all(isinstance(x, pd.DataFrame) for x in value):
                return pd.concat([x.copy() for x in value], ignore_index=True)

            if all(isinstance(x, dict) for x in value):
                rows = []
                for row in value:
                    safe_row = {}
                    for k, v in row.items():
                        if is_scalar_like(v):
                            safe_row[str(k)] = v
                    if safe_row:
                        rows.append(safe_row)
                return pd.DataFrame(rows)

        if is_scalar_like(value):
            return pd.DataFrame([{name: value}])

        return pd.DataFrame()

    except Exception as e:
        logger.error(
            "[summary_pipeline] safe_to_frame failed name=%s err=%s: %s",
            name,
            type(e).__name__,
            str(e)[:300],
            exc_info=False,
        )
        return pd.DataFrame()


def ensure_dataframe(df: Any, name: str = "df") -> pd.DataFrame:
    out = safe_to_frame(df, name=name)

    if out.empty:
        return pd.DataFrame()

    try:
        out = out.copy()
        out.replace([np.inf, -np.inf], np.nan, inplace=True)
    except Exception:
        pass

    return out.reset_index(drop=True)


def coerce_datetime_columns(df: pd.DataFrame) -> pd.DataFrame:
    out = ensure_dataframe(df, "datetime_coerce")
    if out.empty:
        return out

    for col in (
        "datetime",
        "dt",
        "timestamp",
        "end_time",
        "start_time",
        "snapshot_time",
        "received_at",
    ):
        if col in out.columns:
            out[col] = pd.to_datetime(out[col], errors="coerce")
            try:
                out[col] = out[col].dt.tz_localize(None)
            except Exception:
                pass

    return out


def ensure_primary_datetime_col(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()

    if "datetime" in out.columns:
        out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
        try:
            out["datetime"] = out["datetime"].dt.tz_localize(None)
        except Exception:
            pass
        return out

    for c in ("dt", "timestamp", "end_time", "snapshot_time"):
        if c in out.columns:
            out["datetime"] = pd.to_datetime(out[c], errors="coerce")
            try:
                out["datetime"] = out["datetime"].dt.tz_localize(None)
            except Exception:
                pass
            return out

    return out


def latest_only(df: pd.DataFrame) -> pd.DataFrame:
    out = ensure_dataframe(df, "latest_only")
    if out.empty:
        return out

    dt_col = None
    for c in ("datetime", "dt", "timestamp", "end_time", "snapshot_time"):
        if c in out.columns:
            dt_col = c
            break

    if not dt_col or "symbol" not in out.columns:
        return out.reset_index(drop=True)

    out = out.copy()
    out["symbol"] = out["symbol"].astype(str)
    out[dt_col] = pd.to_datetime(out[dt_col], errors="coerce")

    before = len(out)
    out = out.dropna(subset=["symbol", dt_col]).copy()

    if out.empty:
        logger.warning(
            "[summary_pipeline] latest_only became empty before=%s dt_col=%s",
            before,
            dt_col,
        )
        return out

    out = out.sort_values(["symbol", dt_col], kind="stable")
    out = out.groupby("symbol", as_index=False).tail(1)

    logger.info(
        "[summary_pipeline] latest_only done before=%s after=%s symbols=%s dt_col=%s",
        before,
        len(out),
        out["symbol"].nunique(),
        dt_col,
    )

    return out.reset_index(drop=True)


def safe_symbols(df: Any) -> int:
    try:
        if isinstance(df, pd.DataFrame) and not df.empty and "symbol" in df.columns:
            return int(df["symbol"].astype(str).nunique())
    except Exception:
        pass
    return 0


def safe_latest_dt(df: Any):
    try:
        if not isinstance(df, pd.DataFrame) or df.empty:
            return None

        for c in (
            "datetime",
            "dt",
            "timestamp",
            "end_time",
            "start_time",
            "snapshot_time",
            "received_at",
        ):
            if c in df.columns:
                s = pd.to_datetime(df[c], errors="coerce").dropna()
                if not s.empty:
                    ts = s.max()
                    try:
                        ts = ts.tz_localize(None)
                    except Exception:
                        pass
                    return ts
    except Exception:
        pass
    return None


def safe_non_null(df: pd.DataFrame, col: str) -> int:
    try:
        if col in df.columns:
            return int(pd.to_numeric(df[col], errors="coerce").notna().sum())
    except Exception:
        pass
    return 0


def safe_non_zero(df: pd.DataFrame, col: str) -> int:
    try:
        if col in df.columns:
            s = pd.to_numeric(df[col], errors="coerce")
            return int((s.fillna(0) != 0).sum())
    except Exception:
        pass
    return 0


__all__ = [
    "ensure_dataframe",
    "coerce_datetime_columns",
    "ensure_primary_datetime_col",
    "latest_only",
    "safe_symbols",
    "safe_latest_dt",
    "safe_non_null",
    "safe_non_zero",
]
