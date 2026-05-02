# ============================================================
# File   : trading/summary/persistence/core/upsert_guards.py
# Version: Ver1.0-PRODUCTION-UPSERT-GUARDS
# ------------------------------------------------------------
# ✔ invalid OHLC guard
# ✔ dedupe by conflict target
# ✔ table projection
# ============================================================

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from .upsert_normalize import (
    build_time_range_from_datetime,
    cleanup_symbol_series,
    ensure_dataframe,
    ensure_required_identity_columns,
    normalize_datetime_columns,
    pick_price_series,
    safe_get_series,
)

logger = logging.getLogger(__name__)

_SYSTEM_EXCLUDED_COLUMNS = {"id"}


def drop_invalid_ohlc_rows(df: pd.DataFrame, interval: int, stage: str) -> pd.DataFrame:
    df = ensure_dataframe(df)
    if df.empty:
        return df

    open_s = pick_price_series(df, ["open", "open_price"]).mask(lambda s: s <= 0, np.nan)
    high_s = pick_price_series(df, ["high", "high_price"]).mask(lambda s: s <= 0, np.nan)
    low_s = pick_price_series(df, ["low", "low_price"]).mask(lambda s: s <= 0, np.nan)
    close_s = pick_price_series(
        df,
        ["close", "close_price", "price", "current_price", "CurrentPrice", "last_price"]
    ).mask(lambda s: s <= 0, np.nan)

    if int(interval) == 1:
        open_s = open_s.combine_first(close_s)
        high_s = high_s.combine_first(close_s)
        low_s = low_s.combine_first(close_s)

        valid = (
            close_s.notna()
            & open_s.notna()
            & high_s.notna()
            & low_s.notna()
            & (high_s >= low_s)
            & (high_s >= open_s)
            & (high_s >= close_s)
            & (low_s <= open_s)
            & (low_s <= close_s)
        )
    else:
        valid = (
            open_s.notna()
            & high_s.notna()
            & low_s.notna()
            & close_s.notna()
            & (high_s >= low_s)
            & (high_s >= open_s)
            & (high_s >= close_s)
            & (low_s <= open_s)
            & (low_s <= close_s)
        )

    before = len(df)
    bad = df.loc[~valid].copy()

    if not bad.empty:
        sample_cols = [
            c for c in [
                "symbol", "datetime", "date", "time_range",
                "open", "high", "low", "close",
                "open_price", "high_price", "low_price", "close_price",
                "price", "current_price", "CurrentPrice", "last_price",
                "volume",
            ] if c in bad.columns
        ]
        logger.warning(
            "[UPSERT] invalid OHLC removed stage=%s interval=%s removed=%s sample=\n%s",
            stage,
            interval,
            len(bad),
            bad[sample_cols].head(20).to_string(index=False),
        )

    out = df.loc[valid].copy()
    if before != len(out):
        logger.warning(
            "[UPSERT] invalid OHLC removed stage=%s interval=%s before=%d after=%d",
            stage,
            interval,
            before,
            len(out),
        )

    return out


def dedupe_by_conflict_target(df: pd.DataFrame, conflict_target: list[str]) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame() if df is None else df

    missing = [c for c in conflict_target if c not in df.columns]
    if missing:
        logger.warning("[UPSERT] dedupe skipped missing conflict columns=%s", missing)
        return df

    out = df.copy()
    before = len(out)

    try:
        out = out.dropna(subset=conflict_target)
    except Exception:
        logger.debug("[UPSERT] dropna by conflict target failed", exc_info=True)

    try:
        out = out.drop_duplicates(subset=conflict_target, keep="last").reset_index(drop=True)
    except Exception:
        logger.debug("[UPSERT] drop_duplicates by conflict target failed", exc_info=True)

    removed = before - len(out)
    if removed > 0:
        logger.info("[UPSERT] dedupe removed=%s target=%s", removed, conflict_target)

    return out


def project_to_table_columns(
    df: pd.DataFrame,
    table_columns: list[str],
    pk_columns: list[str],
    interval: int,
) -> pd.DataFrame:
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return pd.DataFrame(columns=table_columns)

    out = df.copy()

    out = normalize_datetime_columns(out, interval=interval)
    out = ensure_required_identity_columns(out, interval=interval)
    if out is None or out.empty:
        return pd.DataFrame(columns=table_columns)

    if "symbol" in out.columns:
        try:
            out["symbol"] = cleanup_symbol_series(out["symbol"])
            out = out[
                out["symbol"].notna()
                & (out["symbol"].astype(str).str.strip() != "")
                & (out["symbol"].astype(str).str.lower() != "nan")
                & (out["symbol"].astype(str).str.lower() != "none")
            ].copy()
        except Exception:
            logger.debug("[UPSERT] symbol normalization failed", exc_info=True)

    if out.empty:
        return pd.DataFrame(columns=table_columns)

    protected_cols = {
        "symbol", "datetime", "date", "time_range",
        "time", "end_time", "start_time", "interval", "symbolname"
    }
    excluded = set(_SYSTEM_EXCLUDED_COLUMNS)

    keep_cols: list[str] = []
    for c in table_columns:
        if c not in out.columns:
            continue
        if c in excluded and c not in protected_cols:
            continue
        keep_cols.append(c)

    projected = out[keep_cols].copy()
    projected = ensure_required_identity_columns(projected, interval=interval)

    for c in ("symbol", "datetime", "date", "time_range", "time", "end_time", "start_time", "symbolname"):
        if c in table_columns and c not in projected.columns and c in out.columns:
            try:
                projected[c] = safe_get_series(out, c)
            except Exception:
                logger.debug("[UPSERT] projected key restore failed col=%s", c, exc_info=True)

    try:
        if "datetime" in projected.columns:
            projected["datetime"] = pd.to_datetime(projected["datetime"], errors="coerce")
    except Exception:
        logger.debug("[UPSERT] datetime cast failed", exc_info=True)

    try:
        if "date" in projected.columns:
            projected["date"] = pd.to_datetime(projected["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    except Exception:
        logger.debug("[UPSERT] date cast failed", exc_info=True)

    try:
        for c in ("time", "end_time", "start_time"):
            if c in projected.columns:
                raw = safe_get_series(projected, c)
                if raw is not None:
                    casted = pd.to_datetime(raw, errors="coerce")
                    projected[c] = casted.dt.strftime("%H:%M:%S")
    except Exception:
        logger.debug("[UPSERT] time cast failed", exc_info=True)

    try:
        if "time_range" in projected.columns and "datetime" in projected.columns:
            tr = safe_get_series(projected, "time_range")
            if tr is not None:
                need_fill = tr.isna() | (tr.astype(str).str.strip() == "")
                if need_fill.any():
                    built = build_time_range_from_datetime(projected["datetime"], interval)
                    projected.loc[need_fill, "time_range"] = built.loc[need_fill]
    except Exception:
        logger.debug("[UPSERT] projected time_range refill failed", exc_info=True)

    if "symbol" not in projected.columns:
        logger.error(
            "[UPSERT] projected symbol missing interval=%s keep_cols=%s table_columns=%s pk_columns=%s src_columns=%s",
            interval,
            keep_cols,
            table_columns,
            pk_columns,
            list(out.columns),
        )
    else:
        try:
            projected["symbol"] = cleanup_symbol_series(projected["symbol"])
            projected = projected[
                projected["symbol"].notna()
                & (projected["symbol"].astype(str).str.strip() != "")
                & (projected["symbol"].astype(str).str.lower() != "nan")
                & (projected["symbol"].astype(str).str.lower() != "none")
            ].copy()
        except Exception:
            logger.debug("[UPSERT] projected symbol final normalize failed", exc_info=True)

    projected = drop_invalid_ohlc_rows(projected, interval, "projected-save-guard")

    ordered_cols = [c for c in table_columns if c in projected.columns]
    projected = projected[ordered_cols].copy()
    return projected