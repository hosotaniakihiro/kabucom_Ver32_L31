# ============================================================
# File   : trading/summary/engine/incremental/metrics.py
# Version: Ver1.0-INCREMENTAL-METRICS
# ============================================================

from __future__ import annotations

import pandas as pd

from .common import safe_log_error


def safe_numeric(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    try:
        for col in cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        return df
    except Exception as e:
        safe_log_error("safe_numeric failed", exc=e)
        return df


def rebuild_scaled_slope(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return df

    try:
        df = df.copy()
        df = safe_numeric(df, ["ma75_slope", "atr"])

        if "ma75_slope" not in df.columns:
            return df

        if "atr" in df.columns:
            atr_base = pd.to_numeric(df["atr"], errors="coerce").replace(0, pd.NA)
            df["slope_atr_scaled"] = pd.to_numeric(df["ma75_slope"], errors="coerce") / atr_base
        else:
            df["slope_atr_scaled"] = pd.to_numeric(df["ma75_slope"], errors="coerce")

        df["slope_atr_scaled"] = (
            pd.to_numeric(df["slope_atr_scaled"], errors="coerce")
            .replace([float("inf"), float("-inf")], pd.NA)
            .fillna(0.0)
            .clip(-5, 5)
            .astype("float64")
        )

        df["slope_atr_scaled_1m"] = df["slope_atr_scaled"]
        df["slope_atr_scaled_3m"] = df["slope_atr_scaled"]
        df["slope_atr_scaled_5m"] = df["slope_atr_scaled"]

        return df

    except Exception as e:
        safe_log_error("rebuild_scaled_slope failed", exc=e)
        return df


def ensure_slope(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return df

    try:
        if not {"symbol", "datetime"}.issubset(df.columns):
            return df

        df = df.copy()
        df["symbol"] = df["symbol"].astype(str)
        df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
        df = df.dropna(subset=["symbol", "datetime"])
        df = df.sort_values(["symbol", "datetime"], kind="stable").reset_index(drop=True)

        def _safe_diff(col: str):
            if col not in df.columns:
                return None
            s = pd.to_numeric(df[col], errors="coerce")
            out = (
                s.groupby(df["symbol"])
                .diff()
                .replace([pd.NA, float("inf"), float("-inf")], 0)
                .fillna(0.0)
            )
            return pd.to_numeric(out, errors="coerce").fillna(0.0)

        ma75_s = _safe_diff("ma75")
        volume_s = _safe_diff("volume")
        vwap_s = _safe_diff("vwap")

        if ma75_s is not None:
            df["ma75_slope"] = ma75_s
        if volume_s is not None:
            df["volume_slope"] = volume_s
        if vwap_s is not None:
            df["vwap_slope"] = vwap_s

        return rebuild_scaled_slope(df)

    except Exception as e:
        safe_log_error("slope calc failed", exc=e)
        return df