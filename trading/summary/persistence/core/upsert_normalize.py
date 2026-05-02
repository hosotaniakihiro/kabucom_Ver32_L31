# ============================================================
# File   : trading/summary/persistence/core/upsert_normalize.py
# Version: Ver1.0-PRODUCTION-UPSERT-NORMALIZE
# ------------------------------------------------------------
# ✔ dataframe / symbol / datetime / time_range normalize
# ✔ ohlc alias repair
# ✔ required identity columns 補完
# ============================================================

from __future__ import annotations

import logging
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


def ensure_dataframe(df) -> pd.DataFrame:
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
            logger.exception("[UPSERT] dataframe conversion failed")
            return pd.DataFrame()

    if out.empty:
        return pd.DataFrame()

    try:
        out = out.reset_index(drop=True)
    except Exception:
        pass

    return out


def safe_get_series(df: pd.DataFrame, col: str) -> Optional[pd.Series]:
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
                        try:
                            out = out.where(out.notna(), s)
                        except Exception:
                            pass
            return out

        if isinstance(value, pd.Series):
            return value

        return pd.Series(value, index=df.index)
    except Exception:
        logger.exception("[UPSERT] safe_get_series failed col=%s", col)
        return None


def to_datetime_naive(s) -> pd.Series:
    try:
        out = pd.to_datetime(s, errors="coerce")
        try:
            if getattr(out.dt, "tz", None) is not None:
                out = out.dt.tz_localize(None)
        except Exception:
            pass
        return out
    except Exception:
        return pd.Series(dtype="datetime64[ns]")


def coalesce_first_numeric(df: pd.DataFrame, dest: str, candidates: list[str]) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame() if df is None else df

    out = df.copy()
    if dest not in out.columns:
        out[dest] = pd.NA

    try:
        base = pd.to_numeric(safe_get_series(out, dest), errors="coerce")
    except Exception:
        base = pd.Series(pd.NA, index=out.index)

    for c in candidates:
        if c not in out.columns:
            continue
        try:
            s = pd.to_numeric(safe_get_series(out, c), errors="coerce")
            try:
                base = base.combine_first(s)
            except Exception:
                base = base.where(base.notna(), s)
        except Exception:
            logger.debug("[UPSERT] coalesce numeric failed dest=%s src=%s", dest, c, exc_info=True)

    out[dest] = base
    return out


def cleanup_symbol_series(s: pd.Series) -> pd.Series:
    try:
        out = s.astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
        out = out.replace(
            {"": pd.NA, "nan": pd.NA, "None": pd.NA, "null": pd.NA, "<NA>": pd.NA}
        )
        return out
    except Exception:
        return s


def looks_like_symbol_series(s: pd.Series) -> bool:
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
        ratio = float(hit.mean()) if len(hit) > 0 else 0.0
        return ratio >= 0.7
    except Exception:
        return False


def normalize_symbol(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame() if df is None else df

    out = df.copy()

    try:
        if "symbol" not in out.columns and getattr(out.index, "name", None) == "symbol":
            out = out.reset_index()
    except Exception:
        logger.debug("[UPSERT] reset_index for named symbol failed", exc_info=True)

    try:
        if "symbol" not in out.columns and not isinstance(out.index, pd.RangeIndex):
            out = out.reset_index()
    except Exception:
        logger.debug("[UPSERT] reset_index generic failed", exc_info=True)

    symbol_candidates = [
        "symbol", "Symbol", "SYMBOL",
        "code", "Code", "CODE",
        "ticker", "Ticker", "TICKER",
        "stock_code", "銘柄コード",
        "symbol_x", "symbol_y", "Symbol_x", "Symbol_y",
        "level_0", "index",
    ]

    for col in symbol_candidates:
        if col in out.columns:
            try:
                s = safe_get_series(out, col)
                if s is not None and looks_like_symbol_series(s):
                    out["symbol"] = cleanup_symbol_series(s)
                    break
            except Exception:
                logger.debug("[UPSERT] symbol normalize failed col=%s", col, exc_info=True)

    if "symbol" not in out.columns:
        try:
            for c in out.columns:
                s = safe_get_series(out, c)
                if s is not None and looks_like_symbol_series(s):
                    out["symbol"] = cleanup_symbol_series(s)
                    logger.info("[UPSERT] symbol rescued from column=%s", c)
                    break
        except Exception:
            logger.debug("[UPSERT] symbol rescue scan failed", exc_info=True)

    if "symbol" in out.columns:
        try:
            out["symbol"] = cleanup_symbol_series(out["symbol"])
        except Exception:
            logger.debug("[UPSERT] symbol NA normalize failed", exc_info=True)

    return out


def repair_ohlc_alias(df: pd.DataFrame) -> pd.DataFrame:
    df = ensure_dataframe(df)
    if df.empty:
        return df

    out = df.copy()

    forward_map = {
        "open_price": "open",
        "high_price": "high",
        "low_price": "low",
        "close_price": "close",
    }
    reverse_map = {
        "open": "open_price",
        "high": "high_price",
        "low": "low_price",
        "close": "close_price",
    }

    for src, dst in forward_map.items():
        if src in out.columns and dst not in out.columns:
            s = safe_get_series(out, src)
            if s is not None:
                out[dst] = s

    for src, dst in reverse_map.items():
        if src in out.columns and dst not in out.columns:
            s = safe_get_series(out, src)
            if s is not None:
                out[dst] = s

    out = coalesce_first_numeric(out, "close", [
        "close", "close_price", "price", "current_price", "CurrentPrice", "last_price", "value",
    ])
    out = coalesce_first_numeric(out, "open", [
        "open", "open_price", "opening_price", "opening",
    ])
    out = coalesce_first_numeric(out, "high", [
        "high", "high_price",
    ])
    out = coalesce_first_numeric(out, "low", [
        "low", "low_price",
    ])
    out = coalesce_first_numeric(out, "volume", [
        "volume", "trading_volume", "TradingVolume", "qty", "total_volume",
    ])

    try:
        close_num = pd.to_numeric(safe_get_series(out, "close"), errors="coerce")
        for c in ("open", "high", "low"):
            s = pd.to_numeric(safe_get_series(out, c), errors="coerce")
            try:
                out[c] = s.combine_first(close_num)
            except Exception:
                out[c] = s.where(s.notna(), close_num)
    except Exception:
        logger.debug("[UPSERT] ohlc backfill failed", exc_info=True)

    alias_map = {
        "open_price": "open",
        "high_price": "high",
        "low_price": "low",
        "close_price": "close",
        "price": "close",
        "current_price": "close",
        "last_price": "close",
        "trading_volume": "volume",
    }

    for alias, src in alias_map.items():
        try:
            if alias not in out.columns:
                out[alias] = pd.to_numeric(safe_get_series(out, src), errors="coerce")
            else:
                base = pd.to_numeric(safe_get_series(out, alias), errors="coerce")
                src_s = pd.to_numeric(safe_get_series(out, src), errors="coerce")
                out[alias] = base.combine_first(src_s)
        except Exception:
            logger.debug("[UPSERT] alias sync failed alias=%s src=%s", alias, src, exc_info=True)

    return out


def build_time_range_from_datetime(dt_series: pd.Series, interval: int) -> pd.Series:
    try:
        base = pd.to_datetime(dt_series, errors="coerce")
        start = base.dt.floor(f"{int(interval)}min")
        end = start + pd.to_timedelta(int(interval) - 1, unit="min")
        return start.dt.strftime("%H:%M") + "-" + end.dt.strftime("%H:%M")
    except Exception:
        logger.exception("[UPSERT] build time_range failed interval=%s", interval)
        return pd.Series(pd.NA, index=dt_series.index if hasattr(dt_series, "index") else None)


def normalize_datetime_columns(df: pd.DataFrame, interval: int) -> pd.DataFrame:
    df = ensure_dataframe(df)
    if df.empty:
        return df

    out = normalize_symbol(df)
    out = repair_ohlc_alias(out)

    try:
        if "datetime" in out.columns:
            out["datetime"] = to_datetime_naive(safe_get_series(out, "datetime"))
        else:
            date_col = "date" if "date" in out.columns else None
            time_col = None
            for c in ("time", "end_time", "start_time"):
                if c in out.columns:
                    time_col = c
                    break

            if date_col and time_col:
                ds = safe_get_series(out, date_col)
                ts = safe_get_series(out, time_col)
                out["datetime"] = pd.to_datetime(
                    ds.astype(str) + " " + ts.astype(str),
                    errors="coerce",
                )
            elif time_col:
                out["datetime"] = to_datetime_naive(safe_get_series(out, time_col))
            else:
                out["datetime"] = pd.NaT
    except Exception:
        logger.exception("[UPSERT] datetime normalize failed")

    try:
        if "date" not in out.columns and "datetime" in out.columns:
            out["date"] = pd.to_datetime(out["datetime"], errors="coerce").dt.strftime("%Y-%m-%d")
        elif "date" in out.columns:
            out["date"] = pd.to_datetime(safe_get_series(out, "date"), errors="coerce").dt.strftime("%Y-%m-%d")
    except Exception:
        logger.debug("[UPSERT] derive/normalize date failed", exc_info=True)

    try:
        if "time" not in out.columns and "datetime" in out.columns:
            out["time"] = pd.to_datetime(out["datetime"], errors="coerce").dt.strftime("%H:%M:%S")
        elif "time" in out.columns:
            raw = safe_get_series(out, "time")
            if raw is not None:
                t = pd.to_datetime(raw, errors="coerce")
                out["time"] = t.dt.strftime("%H:%M:%S")
    except Exception:
        logger.debug("[UPSERT] derive/normalize time failed", exc_info=True)

    try:
        if "end_time" not in out.columns and "datetime" in out.columns:
            out["end_time"] = pd.to_datetime(out["datetime"], errors="coerce").dt.strftime("%H:%M:%S")
    except Exception:
        logger.debug("[UPSERT] derive end_time failed", exc_info=True)

    try:
        if "start_time" not in out.columns and "datetime" in out.columns:
            floored = pd.to_datetime(out["datetime"], errors="coerce").dt.floor(f"{int(interval)}min")
            out["start_time"] = floored.dt.strftime("%H:%M:%S")
    except Exception:
        logger.debug("[UPSERT] derive start_time failed", exc_info=True)

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
                    | tr_str.isin(["1min", "3min", "5min", "10min", "15min", "30min", "60min"])
                )
                if need_fill.any() and "datetime" in out.columns:
                    built = build_time_range_from_datetime(out["datetime"], interval)
                    out.loc[need_fill, "time_range"] = built.loc[need_fill]
    except Exception:
        logger.debug("[UPSERT] derive/normalize time_range failed", exc_info=True)

    try:
        if "symbol" in out.columns and "datetime" in out.columns:
            out = out.sort_values(["symbol", "datetime"], kind="stable").reset_index(drop=True)
    except Exception:
        logger.debug("[UPSERT] final sort failed", exc_info=True)

    return out


def ensure_symbol_present(df: pd.DataFrame) -> pd.DataFrame:
    try:
        if df is None or getattr(df, "empty", True):
            return df

        out = df.copy()
        out = normalize_symbol(out)

        if "symbol" not in out.columns:
            return out

        out["symbol"] = cleanup_symbol_series(out["symbol"])
        out = out[out["symbol"].notna()].copy()

        return out

    except Exception:
        logger.exception("[UPSERT] ensure_symbol_present failed")
        return df


def ensure_required_identity_columns(df: pd.DataFrame, interval: int) -> pd.DataFrame:
    out = ensure_dataframe(df)
    if out.empty:
        return out

    out = ensure_symbol_present(out)
    out = normalize_datetime_columns(out, interval=interval)

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
        logger.debug("[UPSERT] ensure date failed", exc_info=True)

    try:
        if "time" not in out.columns and "datetime" in out.columns:
            out["time"] = pd.to_datetime(out["datetime"], errors="coerce").dt.strftime("%H:%M:%S")
    except Exception:
        logger.debug("[UPSERT] ensure time failed", exc_info=True)

    try:
        if "end_time" not in out.columns and "datetime" in out.columns:
            out["end_time"] = pd.to_datetime(out["datetime"], errors="coerce").dt.strftime("%H:%M:%S")
    except Exception:
        logger.debug("[UPSERT] ensure end_time failed", exc_info=True)

    try:
        if "start_time" not in out.columns and "datetime" in out.columns:
            floored = pd.to_datetime(out["datetime"], errors="coerce").dt.floor(f"{int(interval)}min")
            out["start_time"] = floored.dt.strftime("%H:%M:%S")
    except Exception:
        logger.debug("[UPSERT] ensure start_time failed", exc_info=True)

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
                    | tr_str.isin(["1min", "3min", "5min", "10min", "15min", "30min", "60min"])
                )
                if need_fill.any() and "datetime" in out.columns:
                    built = build_time_range_from_datetime(out["datetime"], interval)
                    out.loc[need_fill, "time_range"] = built.loc[need_fill]
    except Exception:
        logger.debug("[UPSERT] ensure time_range failed", exc_info=True)

    try:
        if "symbol" in out.columns:
            out["symbol"] = cleanup_symbol_series(out["symbol"])
    except Exception:
        logger.debug("[UPSERT] final symbol cleanup failed", exc_info=True)

    return out


def pick_price_series(df: pd.DataFrame, candidates: list[str]) -> pd.Series:
    idx = df.index
    base = pd.Series(np.nan, index=idx, dtype="float64")
    for c in candidates:
        if c in df.columns:
            try:
                s = pd.to_numeric(safe_get_series(df, c), errors="coerce").replace([np.inf, -np.inf], np.nan)
                base = base.combine_first(s)
            except Exception:
                logger.debug("[UPSERT] pick price failed col=%s", c, exc_info=True)
    return base