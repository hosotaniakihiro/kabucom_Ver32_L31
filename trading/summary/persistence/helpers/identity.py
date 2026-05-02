# ============================================================
# File   : trading/summary/persistence/helpers/identity.py
# Version: Ver1.0-SUMMARY-IDENTITY
# ------------------------------------------------------------
# symbol / datetime / OHLC alias 補修
# ============================================================

from __future__ import annotations

import logging
import pandas as pd

from .dataframe_utils import (
    _ensure_dataframe,
    _safe_get_series,
    _to_datetime_naive,
    _normalize_symbol,
    _coalesce_numeric,
    _build_time_range_from_datetime,
    _build_start_time,
    _build_end_time,
)

logger = logging.getLogger(__name__)


def _repair_ohlc_alias(df: pd.DataFrame) -> pd.DataFrame:
    out = _ensure_dataframe(df)
    if out.empty:
        return out

    out = _coalesce_numeric(out, "close", [
        "close", "close_price", "price", "current_price", "CurrentPrice", "last_price"
    ])
    out = _coalesce_numeric(out, "open", [
        "open", "open_price"
    ])
    out = _coalesce_numeric(out, "high", [
        "high", "high_price"
    ])
    out = _coalesce_numeric(out, "low", [
        "low", "low_price"
    ])
    out = _coalesce_numeric(out, "volume", [
        "volume", "trading_volume", "qty", "total_volume"
    ])

    try:
        close_num = pd.to_numeric(_safe_get_series(out, "close"), errors="coerce")
        for c in ("open", "high", "low"):
            s = pd.to_numeric(_safe_get_series(out, c), errors="coerce")
            try:
                out[c] = s.combine_first(close_num)
            except Exception:
                out[c] = s.where(s.notna(), close_num)
    except Exception:
        logger.debug("[SUMMARY] OHLC backfill failed", exc_info=True)

    alias_map = {
        "open_price": "open",
        "high_price": "high",
        "low_price": "low",
        "close_price": "close",
    }
    for alias, src in alias_map.items():
        try:
            out[alias] = pd.to_numeric(_safe_get_series(out, src), errors="coerce")
        except Exception:
            logger.debug("[SUMMARY] alias sync failed alias=%s src=%s", alias, src, exc_info=True)

    return out


def _ensure_identity_columns(df: pd.DataFrame, interval: int) -> pd.DataFrame:
    out = _ensure_dataframe(df)
    if out.empty:
        return out

    out = _normalize_symbol(out)
    out = _repair_ohlc_alias(out)

    try:
        if "datetime" in out.columns:
            out["datetime"] = _to_datetime_naive(_safe_get_series(out, "datetime"))
        else:
            date_col = "date" if "date" in out.columns else None
            time_col = None
            for c in ("time", "end_time", "start_time"):
                if c in out.columns:
                    time_col = c
                    break

            if date_col and time_col:
                ds = _safe_get_series(out, date_col)
                ts = _safe_get_series(out, time_col)
                out["datetime"] = pd.to_datetime(
                    ds.astype(str) + " " + ts.astype(str),
                    errors="coerce",
                )
            else:
                out["datetime"] = pd.NaT
    except Exception:
        logger.exception("[SUMMARY] datetime normalization failed")

    try:
        out["date"] = pd.to_datetime(out["datetime"], errors="coerce").dt.strftime("%Y-%m-%d")
    except Exception:
        logger.debug("[SUMMARY] date normalize failed", exc_info=True)

    try:
        out["time"] = pd.to_datetime(out["datetime"], errors="coerce").dt.strftime("%H:%M:%S")
    except Exception:
        logger.debug("[SUMMARY] time normalize failed", exc_info=True)

    try:
        out["start_time"] = _build_start_time(out["datetime"], interval)
    except Exception:
        logger.debug("[SUMMARY] start_time normalize failed", exc_info=True)

    try:
        out["end_time"] = _build_end_time(out["datetime"])
    except Exception:
        logger.debug("[SUMMARY] end_time normalize failed", exc_info=True)

    try:
        out["time_range"] = _build_time_range_from_datetime(out["datetime"], interval)
    except Exception:
        logger.debug("[SUMMARY] time_range normalize failed", exc_info=True)

    if "symbolname" not in out.columns:
        out["symbolname"] = ""
    out["symbolname"] = out["symbolname"].fillna("").astype(str).str.strip()

    if "source" not in out.columns:
        out["source"] = ""

    if "interval" not in out.columns:
        out["interval"] = int(interval)

    return out