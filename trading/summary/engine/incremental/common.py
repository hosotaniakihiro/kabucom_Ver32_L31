# ============================================================
# File   : trading/summary/engine/incremental/common.py
# Version: Ver1.0-INCREMENTAL-COMMON
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Optional

import pandas as pd

from trading.summary.persistence.summary_persistence import bulk_upsert_summary

logger = logging.getLogger(__name__)


def empty_result(interval: int) -> dict:
    return {
        "interval": int(interval),
        "summary_df": pd.DataFrame(),
        "summary_latest_df": pd.DataFrame(),
    }


def interval_label(interval: int) -> str:
    interval = int(interval)
    if interval == 1:
        return "1min"
    if interval == 3:
        return "3min"
    if interval == 5:
        return "5min"
    return f"{interval}min"


def safe_log_error(msg: str, *args, exc: Exception | None = None) -> None:
    try:
        if exc is None:
            logger.error(msg, *args, exc_info=False)
        else:
            logger.error(
                (msg + " err=%s: %s"),
                *args,
                type(exc).__name__,
                str(exc)[:300],
                exc_info=False,
            )
    except Exception:
        logger.error(msg, *args, exc_info=False)


def to_dataframe(obj: Any) -> pd.DataFrame:
    try:
        if obj is None:
            return pd.DataFrame()
        if isinstance(obj, pd.DataFrame):
            return obj.copy()
        if isinstance(obj, pd.Series):
            return pd.DataFrame([obj.to_dict()])
        if isinstance(obj, dict):
            try:
                return pd.DataFrame([obj]).copy()
            except Exception:
                return pd.DataFrame()
        return pd.DataFrame(obj).copy()
    except Exception:
        return pd.DataFrame()


def safe_len(obj: Any) -> int:
    try:
        return int(len(obj))
    except Exception:
        return 0


def today_date() -> dt.date:
    return dt.datetime.now().date()


def now_naive() -> dt.datetime:
    return dt.datetime.now().replace(tzinfo=None)


def safe_getattr(obj: Any, name: str, default=None):
    try:
        return getattr(obj, name, default)
    except Exception:
        return default


def normalize_datetime_col(df: pd.DataFrame, col: str = "datetime") -> pd.DataFrame:
    try:
        if df is None or df.empty:
            return df
        out = df.copy()
        if col in out.columns:
            out[col] = pd.to_datetime(out[col], errors="coerce")
            try:
                out[col] = out[col].dt.tz_localize(None)
            except Exception:
                pass
        return out
    except Exception:
        return df


def extract_latest_ts(df: pd.DataFrame) -> Optional[pd.Timestamp]:
    try:
        if df is None or df.empty:
            return None
        for c in ("datetime", "end_time", "time", "start_time", "snapshot_time"):
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


def numeric_abs_sum(df: pd.DataFrame, cols: list[str]) -> float:
    if df is None or df.empty:
        return 0.0

    use_cols = [c for c in cols if c in df.columns]
    if not use_cols:
        return 0.0

    try:
        x = df[use_cols].copy()
        for c in use_cols:
            x[c] = pd.to_numeric(x[c], errors="coerce").fillna(0.0)
        return float(x.abs().sum().sum())
    except Exception:
        return 0.0


def looks_uncomputed_df(df: pd.DataFrame) -> bool:
    if df is None or df.empty:
        return True

    score_cols = [
        "score", "score_total", "final_score", "display_score",
        "score_buy", "score_sell", "score_slope", "score_mtf",
    ]
    tech_cols = [
        "slope", "slope_atr_scaled", "ma75_slope",
        "mtf", "mtf_score",
        "rsi", "macd", "signal", "hist",
    ]

    score_abs = numeric_abs_sum(df, score_cols)
    tech_abs = numeric_abs_sum(df, tech_cols)

    has_price = False
    try:
        if "close" in df.columns:
            has_price = pd.to_numeric(df["close"], errors="coerce").notna().any()
    except Exception:
        has_price = False

    return bool(has_price and score_abs == 0.0 and tech_abs == 0.0)


def profile_numeric_state(label: str, df: pd.DataFrame) -> None:
    try:
        if df is None or df.empty:
            logger.warning("[SUMMARY PROFILE] %s empty", label)
            return

        cols = ["score", "rsi", "macd", "signal", "hist", "slope", "mtf", "score_slope", "score_mtf"]
        use_cols = [c for c in cols if c in df.columns]

        if not use_cols:
            logger.info("[SUMMARY PROFILE] %s no-target-cols", label)
            return

        info = {}
        for c in use_cols:
            s = pd.to_numeric(df[c], errors="coerce")
            info[c] = {
                "nonnull": int(s.notna().sum()),
                "nonzero": int((s.fillna(0.0) != 0).sum()),
                "abs_sum": round(float(s.fillna(0.0).abs().sum()), 6),
            }

        logger.info("[SUMMARY PROFILE] %s %s", label, info)
    except Exception as e:
        safe_log_error("[SUMMARY PROFILE] failed label=%s", label, exc=e)


def log_df_state(label: str, df: pd.DataFrame) -> None:
    try:
        if df is None:
            logger.warning("[SUMMARY STATE] %s: df is None", label)
            return

        rows = len(df)
        cols = len(df.columns)

        latest_dt = None
        if "datetime" in df.columns and not df.empty:
            s = pd.to_datetime(df["datetime"], errors="coerce").dropna()
            if not s.empty:
                latest_dt = s.max()

        dup_count = 0
        if {"symbol", "datetime"}.issubset(df.columns) and not df.empty:
            temp = df.copy()
            temp["symbol"] = temp["symbol"].astype(str)
            temp["datetime"] = pd.to_datetime(temp["datetime"], errors="coerce")
            temp = temp.dropna(subset=["symbol", "datetime"])
            dup_count = int(temp.duplicated(subset=["symbol", "datetime"], keep=False).sum())

        logger.info(
            "[SUMMARY STATE] %s rows=%d cols=%d latest_dt=%s dup(symbol,datetime)=%d",
            label,
            rows,
            cols,
            latest_dt,
            dup_count,
        )
    except Exception as e:
        safe_log_error("[SUMMARY STATE] logging failed: %s", label, exc=e)


def safe_upsert(df: pd.DataFrame, interval: int) -> None:
    if df is None or df.empty:
        logger.warning("[SUMMARY] skip empty upsert %smin", interval)
        return

    try:
        log_df_state(f"pre-upsert-{interval}min", df)
        bulk_upsert_summary(df, interval)
        logger.info("[SUMMARY] upsert success %smin rows=%d", interval, len(df))
    except Exception as e:
        safe_log_error("[SUMMARY] upsert failed %smin", interval, exc=e)