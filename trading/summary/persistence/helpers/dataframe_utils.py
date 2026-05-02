# ============================================================
# File   : trading/summary/persistence/helpers/dataframe_utils.py
# Version: Ver1.0-SUMMARY-DATAFRAME-UTILS
# ------------------------------------------------------------
# DataFrame 基本整形 / symbol / datetime helper
# ============================================================

from __future__ import annotations

import logging
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)


def _ensure_dataframe(df) -> pd.DataFrame:
    if df is None:
        return pd.DataFrame()

    if isinstance(df, pd.DataFrame):
        out = df.copy()
    elif isinstance(df, pd.Series):
        out = pd.DataFrame([df.to_dict()])
    elif isinstance(df, dict):
        out = pd.DataFrame([df])
    else:
        try:
            out = pd.DataFrame(df).copy()
        except Exception:
            logger.exception("[SUMMARY] dataframe conversion failed")
            return pd.DataFrame()

    if out.empty:
        return pd.DataFrame()

    try:
        out = out.reset_index(drop=True)
    except Exception:
        pass

    try:
        out = out.loc[:, ~out.columns.duplicated()].copy()
    except Exception:
        pass

    return out


def _safe_get_series(df: pd.DataFrame, col: str) -> Optional[pd.Series]:
    try:
        if df is None or df.empty or col not in df.columns:
            return None

        value = df[col]

        if isinstance(value, pd.DataFrame):
            if value.shape[1] <= 0:
                return None
            out = None
            for i in range(value.shape[1]):
                s = value.iloc[:, i]
                if out is None:
                    out = s
                else:
                    try:
                        out = out.combine_first(s)
                    except Exception:
                        out = out.where(out.notna(), s)
            return out

        if isinstance(value, pd.Series):
            return value

        return pd.Series(value, index=df.index)
    except Exception:
        logger.exception("[SUMMARY] safe_get_series failed col=%s", col)
        return None


def _to_datetime_naive(s) -> pd.Series:
    try:
        out = pd.to_datetime(s, errors="coerce")
        try:
            if getattr(out.dt, "tz", None) is not None:
                try:
                    out = out.dt.tz_convert(None)
                except Exception:
                    out = out.dt.tz_localize(None)
        except Exception:
            pass
        return out
    except Exception:
        return pd.Series(dtype="datetime64[ns]")


def _cleanup_symbol_series(s: pd.Series) -> pd.Series:
    try:
        out = s.astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
        out = out.replace(
            {"": pd.NA, "nan": pd.NA, "None": pd.NA, "null": pd.NA, "<NA>": pd.NA}
        )
        return out
    except Exception:
        return s


def _looks_like_symbol_series(s: pd.Series) -> bool:
    try:
        if s is None:
            return False
        x = s.astype(str).str.strip()
        x = x.replace(
            {"": pd.NA, "nan": pd.NA, "None": pd.NA, "null": pd.NA, "<NA>": pd.NA}
        ).dropna()
        if x.empty:
            return False
        hit = x.str.match(r"^[0-9]{4}[A-Z]?$", na=False)
        return float(hit.mean()) >= 0.7
    except Exception:
        return False


def _normalize_symbol(df: pd.DataFrame) -> pd.DataFrame:
    out = _ensure_dataframe(df)
    if out.empty:
        return out

    try:
        if "symbol" not in out.columns and getattr(out.index, "name", None) == "symbol":
            out = out.reset_index()
    except Exception:
        logger.debug("[SUMMARY] reset_index for symbol failed", exc_info=True)

    try:
        if "symbol" not in out.columns and not isinstance(out.index, pd.RangeIndex):
            out = out.reset_index()
    except Exception:
        logger.debug("[SUMMARY] reset_index generic failed", exc_info=True)

    candidates = [
        "symbol", "Symbol", "SYMBOL",
        "code", "Code", "CODE",
        "ticker", "Ticker", "TICKER",
        "stock_code", "銘柄コード",
        "symbol_x", "symbol_y",
        "level_0", "index",
    ]

    for col in candidates:
        if col in out.columns:
            try:
                s = _safe_get_series(out, col)
                if s is not None and _looks_like_symbol_series(s):
                    out["symbol"] = _cleanup_symbol_series(s)
                    break
            except Exception:
                logger.debug("[SUMMARY] symbol normalize failed col=%s", col, exc_info=True)

    if "symbol" in out.columns:
        try:
            out["symbol"] = _cleanup_symbol_series(out["symbol"])
            out = out[out["symbol"].notna()].copy()
        except Exception:
            logger.debug("[SUMMARY] symbol cleanup failed", exc_info=True)

    return out


def _coalesce_numeric(out: pd.DataFrame, dest: str, srcs: list[str]) -> pd.DataFrame:
    if dest not in out.columns:
        out[dest] = pd.NA

    try:
        base = pd.to_numeric(_safe_get_series(out, dest), errors="coerce")
    except Exception:
        base = pd.Series(pd.NA, index=out.index)

    for c in srcs:
        if c not in out.columns:
            continue
        try:
            s = pd.to_numeric(_safe_get_series(out, c), errors="coerce")
            try:
                base = base.combine_first(s)
            except Exception:
                base = base.where(base.notna(), s)
        except Exception:
            logger.debug("[SUMMARY] coalesce numeric failed dest=%s src=%s", dest, c, exc_info=True)

    out[dest] = base
    return out


def _build_time_range_from_datetime(dt_series: pd.Series, interval: int) -> pd.Series:
    try:
        base = pd.to_datetime(dt_series, errors="coerce")
        start = base.dt.floor(f"{int(interval)}min")
        end = start + pd.to_timedelta(int(interval) - 1, unit="min")
        return start.dt.strftime("%H:%M") + "-" + end.dt.strftime("%H:%M")
    except Exception:
        logger.exception("[SUMMARY] build time_range failed interval=%s", interval)
        return pd.Series(pd.NA, index=dt_series.index if hasattr(dt_series, "index") else None)


def _build_start_time(dt_series: pd.Series, interval: int) -> pd.Series:
    try:
        base = pd.to_datetime(dt_series, errors="coerce")
        start = base.dt.floor(f"{int(interval)}min")
        return start.dt.strftime("%H:%M:%S")
    except Exception:
        logger.exception("[SUMMARY] build start_time failed interval=%s", interval)
        return pd.Series(pd.NA, index=dt_series.index if hasattr(dt_series, "index") else None)


def _build_end_time(dt_series: pd.Series) -> pd.Series:
    try:
        base = pd.to_datetime(dt_series, errors="coerce")
        return base.dt.strftime("%H:%M:%S")
    except Exception:
        logger.exception("[SUMMARY] build end_time failed")
        return pd.Series(pd.NA, index=dt_series.index if hasattr(dt_series, "index") else None)