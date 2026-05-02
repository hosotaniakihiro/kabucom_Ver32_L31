# ============================================================
# File   : trading/summary/persistence/identity_builder.py
# Version: Ver1.0-PRODUCTION-IDENTITY-BUILDER
# ------------------------------------------------------------
# 機能:
# - datetime/date/time/start_time/end_time 補完
# - time_range 生成
# - symbol / symbolname / interval の整備
# - summary保存用 identity 列の保証
# ------------------------------------------------------------
# 主な責務:
# - DB保存前に必要なキー列を整える
# - symbol/date/time_range/datetime の整合を取る
# ============================================================

from __future__ import annotations

import logging

import pandas as pd

from trading.summary.persistence.dataframe_utils import (
    cleanup_symbol_series,
    coalesce_duplicate_columns,
    ensure_dataframe,
    normalize_symbol,
    repair_ohlc_alias,
    safe_get_series,
    to_datetime_naive,
)

logger = logging.getLogger(__name__)


def build_time_range_from_datetime(dt_series: pd.Series, interval: int) -> pd.Series:
    try:
        base = pd.to_datetime(dt_series, errors="coerce")
        start = base.dt.floor(f"{int(interval)}min")
        end = start + pd.to_timedelta(int(interval) - 1, unit="min")
        return start.dt.strftime("%H:%M") + "-" + end.dt.strftime("%H:%M")
    except Exception:
        logger.exception("[SUMMARY] build time_range failed interval=%s", interval)
        return pd.Series(pd.NA, index=dt_series.index if hasattr(dt_series, "index") else None)


def normalize_datetime_columns(df: pd.DataFrame) -> pd.DataFrame:
    df = ensure_dataframe(df)
    if df.empty:
        return df

    out = df.copy()
    out = coalesce_duplicate_columns(out)
    out = normalize_symbol(out)
    out = repair_ohlc_alias(out)

    try:
        if "datetime" in out.columns:
            out["datetime"] = to_datetime_naive(safe_get_series(out, "datetime"))
        else:
            date_col = "date" if "date" in out.columns else None
            time_col = None
            for c in ("time", "end_time", "start_time", "snapshot_time"):
                if c in out.columns:
                    time_col = c
                    break

            if date_col and time_col:
                out["datetime"] = pd.to_datetime(
                    safe_get_series(out, date_col).astype(str) + " " + safe_get_series(out, time_col).astype(str),
                    errors="coerce",
                )
            elif time_col:
                out["datetime"] = to_datetime_naive(safe_get_series(out, time_col))
            else:
                out["datetime"] = pd.NaT
    except Exception:
        logger.exception("[SUMMARY] datetime normalize failed")

    try:
        if "date" not in out.columns and "datetime" in out.columns:
            out["date"] = pd.to_datetime(out["datetime"], errors="coerce").dt.strftime("%Y-%m-%d")
        elif "date" in out.columns:
            out["date"] = pd.to_datetime(safe_get_series(out, "date"), errors="coerce").dt.strftime("%Y-%m-%d")
    except Exception:
        logger.debug("[SUMMARY] date normalize failed", exc_info=True)

    try:
        if "time" not in out.columns and "datetime" in out.columns:
            out["time"] = pd.to_datetime(out["datetime"], errors="coerce").dt.strftime("%H:%M:%S")
        elif "time" in out.columns:
            raw = safe_get_series(out, "time")
            dt = pd.to_datetime(raw, errors="coerce")
            out["time"] = dt.dt.strftime("%H:%M:%S")
            mask = dt.isna()
            if mask.any():
                out.loc[mask, "time"] = raw.astype(str).str.extract(r"(\d{2}:\d{2}:\d{2})", expand=False)
    except Exception:
        logger.debug("[SUMMARY] time normalize failed", exc_info=True)

    try:
        if "end_time" not in out.columns and "datetime" in out.columns:
            out["end_time"] = pd.to_datetime(out["datetime"], errors="coerce").dt.strftime("%H:%M:%S")
        elif "end_time" in out.columns:
            raw = safe_get_series(out, "end_time")
            dt = pd.to_datetime(raw, errors="coerce")
            out["end_time"] = dt.dt.strftime("%H:%M:%S")
            mask = dt.isna()
            if mask.any():
                out.loc[mask, "end_time"] = raw.astype(str).str.extract(r"(\d{2}:\d{2}:\d{2})", expand=False)
    except Exception:
        logger.debug("[SUMMARY] end_time normalize failed", exc_info=True)

    try:
        if "start_time" not in out.columns and "datetime" in out.columns:
            floored = pd.to_datetime(out["datetime"], errors="coerce").dt.floor("min")
            out["start_time"] = floored.dt.strftime("%H:%M:%S")
        elif "start_time" in out.columns:
            raw = safe_get_series(out, "start_time")
            dt = pd.to_datetime(raw, errors="coerce")
            out["start_time"] = dt.dt.strftime("%H:%M:%S")
            mask = dt.isna()
            if mask.any():
                out.loc[mask, "start_time"] = raw.astype(str).str.extract(r"(\d{2}:\d{2}:\d{2})", expand=False)
    except Exception:
        logger.debug("[SUMMARY] start_time normalize failed", exc_info=True)

    try:
        sort_cols = []
        if "symbol" in out.columns:
            sort_cols.append("symbol")
        if "datetime" in out.columns:
            sort_cols.append("datetime")
        if sort_cols:
            out = out.sort_values(sort_cols, kind="stable").reset_index(drop=True)
    except Exception:
        logger.debug("[SUMMARY] sort failed", exc_info=True)

    return out


def ensure_required_identity_columns(df: pd.DataFrame, interval: int) -> pd.DataFrame:
    out = ensure_dataframe(df)
    if out.empty:
        return out

    out = normalize_datetime_columns(out)

    try:
        if "interval" not in out.columns:
            out["interval"] = int(interval)
    except Exception:
        pass

    if "symbolname" not in out.columns:
        out["symbolname"] = ""

    try:
        if "date" not in out.columns and "datetime" in out.columns:
            out["date"] = pd.to_datetime(out["datetime"], errors="coerce").dt.strftime("%Y-%m-%d")
    except Exception:
        logger.debug("[SUMMARY] ensure date failed", exc_info=True)

    try:
        if "time" not in out.columns and "datetime" in out.columns:
            out["time"] = pd.to_datetime(out["datetime"], errors="coerce").dt.strftime("%H:%M:%S")
    except Exception:
        logger.debug("[SUMMARY] ensure time failed", exc_info=True)

    try:
        if "end_time" not in out.columns and "datetime" in out.columns:
            out["end_time"] = pd.to_datetime(out["datetime"], errors="coerce").dt.strftime("%H:%M:%S")
    except Exception:
        logger.debug("[SUMMARY] ensure end_time failed", exc_info=True)

    try:
        if "start_time" not in out.columns and "datetime" in out.columns:
            floored = pd.to_datetime(out["datetime"], errors="coerce").dt.floor(f"{int(interval)}min")
            out["start_time"] = floored.dt.strftime("%H:%M:%S")
    except Exception:
        logger.debug("[SUMMARY] ensure start_time failed", exc_info=True)

    try:
        if "time_range" not in out.columns and "datetime" in out.columns:
            out["time_range"] = build_time_range_from_datetime(out["datetime"], interval)
        elif "time_range" in out.columns:
            tr = safe_get_series(out, "time_range")
            if tr is not None:
                tr_str = tr.astype(str)
                need_fill = (
                    tr.isna()
                    | (tr_str.str.strip() == "")
                    | tr_str.isin(["1min", "3min", "5min", "10min", "15min", "30min", "60min", "unknown"])
                )
                if need_fill.any() and "datetime" in out.columns:
                    built = build_time_range_from_datetime(out["datetime"], interval)
                    out.loc[need_fill, "time_range"] = built.loc[need_fill]
    except Exception:
        logger.debug("[SUMMARY] ensure time_range failed", exc_info=True)

    try:
        if "symbol" in out.columns:
            out["symbol"] = cleanup_symbol_series(out["symbol"])
            out = out[
                out["symbol"].notna()
                & (out["symbol"].astype(str).str.strip() != "")
                & (out["symbol"].astype(str).str.lower() != "nan")
                & (out["symbol"].astype(str).str.lower() != "none")
            ].copy()
    except Exception:
        logger.debug("[SUMMARY] ensure symbol cleanup failed", exc_info=True)

    return out