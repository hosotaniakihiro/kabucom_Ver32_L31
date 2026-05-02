# ============================================================
# File   : trading/scoring/core/preprocess/column_normalizer.py
# Version: Ver3.0-PRODUCTION-DUPLICATE-DF-COLUMN-NORMALIZER-FINAL
# ------------------------------------------------------------
# ✔ duplicate label coalesce
# ✔ DataFrame-valued duplicate columns safe
# ✔ OHLCV alias normalization
# ✔ datetime / symbol / symbolname normalize
# ✔ numeric-safe / NaN-safe / inf-safe
# ✔ scoring_pipeline compatible
# ✔ production hardened
# ============================================================

from __future__ import annotations

import logging
from typing import Iterable, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

_OPEN_ALIASES = ("open", "open_price", "openPrice", "始値", "openValue", "opening_price")
_HIGH_ALIASES = ("high", "high_price", "highPrice", "高値", "highValue")
_LOW_ALIASES = ("low", "low_price", "lowPrice", "安値", "lowValue")
_CLOSE_ALIASES = (
    "close", "close_price", "closePrice", "終値", "closeValue",
    "price", "current_price", "CurrentPrice", "currentPrice",
    "last_price", "last", "value", "currentvalue",
)
_VOLUME_ALIASES = ("volume", "trading_volume", "出来高", "volume_total", "vol", "qty")
_SYMBOL_ALIASES = ("symbol", "code", "ticker", "stock_code", "Symbol")
_SYMBOLNAME_ALIASES = ("symbolname", "name", "display_name", "銘柄名")
_DATETIME_ALIASES = ("datetime", "end_time", "start_time", "time", "snapshot_time")


# ============================================================
# low-level safe helpers
# ============================================================

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
            return pd.DataFrame()

    if out.empty:
        return pd.DataFrame()

    try:
        if isinstance(out.columns, pd.MultiIndex):
            out.columns = [
                "_".join([str(x) for x in col if str(x) != ""])
                for col in out.columns.to_flat_index()
            ]
    except Exception:
        pass

    try:
        out.columns = [str(c) for c in out.columns]
    except Exception:
        pass

    try:
        out = out.reset_index(drop=True)
    except Exception:
        pass

    return out


def _extract_series(df: pd.DataFrame, col: str) -> Optional[pd.Series]:
    """
    df[col] が duplicate label のため DataFrame になっても、
    1本の Series に安全統合して返す。
    """
    try:
        if col not in df.columns:
            return None

        obj = df[col]

        if isinstance(obj, pd.Series):
            return obj

        if isinstance(obj, pd.DataFrame):
            if obj.shape[1] == 0:
                return None

            logger.warning(
                "[SCORING PIPELINE] duplicate label coalesced -> %s count=%d",
                col,
                obj.shape[1],
            )

            base = obj.iloc[:, 0]
            for i in range(1, obj.shape[1]):
                cur = obj.iloc[:, i]
                try:
                    base = base.where(base.notna(), cur)
                except Exception:
                    try:
                        base = base.combine_first(cur)
                    except Exception:
                        pass
            return base

        return pd.Series(obj, index=df.index)

    except Exception:
        logger.exception("[SCORING PIPELINE] extract series failed col=%s", col)
        return None


def _to_numeric_series(s: Optional[pd.Series], index, default: float = np.nan) -> pd.Series:
    if s is None:
        return pd.Series(default, index=index, dtype="float64")
    try:
        out = pd.to_numeric(s, errors="coerce")
        out = out.replace([np.inf, -np.inf], np.nan)
        return out.reindex(index)
    except Exception:
        return pd.Series(default, index=index, dtype="float64")


def _to_text_series(s: Optional[pd.Series], index, default: str = "") -> pd.Series:
    if s is None:
        return pd.Series(default, index=index, dtype="object")
    try:
        out = s.astype(str).replace({"nan": default, "None": default, "<NA>": default})
        return out.reindex(index).fillna(default)
    except Exception:
        return pd.Series(default, index=index, dtype="object")


def _coalesce_first_nonnull(df: pd.DataFrame, cols: Iterable[str], numeric: bool = True):
    idx = df.index
    cols = [c for c in cols if c in df.columns]
    if not cols:
        return pd.Series(np.nan if numeric else "", index=idx)

    base = None
    for c in cols:
        s = _extract_series(df, c)
        if numeric:
            cur = _to_numeric_series(s, idx, default=np.nan)
        else:
            cur = _to_text_series(s, idx, default="")

        if base is None:
            base = cur
            continue

        try:
            if numeric:
                base = base.where(base.notna(), cur)
            else:
                base = base.where(base.astype(str).str.strip().ne(""), cur)
        except Exception:
            try:
                base = base.combine_first(cur)
            except Exception:
                pass

    if base is None:
        return pd.Series(np.nan if numeric else "", index=idx)

    return base


def _coalesce_duplicate_labels(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df

    dup_names = [c for c in df.columns if list(df.columns).count(c) > 1]
    if dup_names:
        logger.warning(
            "[SCORING PIPELINE] duplicate columns detected -> %s",
            sorted(set(dup_names)),
        )

    canonical_targets = (
        "symbol", "symbolname", "datetime",
        "open", "high", "low", "close", "volume",
        "open_price", "high_price", "low_price", "close_price",
        "trading_volume",
    )

    out = df.copy()
    for col in canonical_targets:
        if col in out.columns:
            s = _extract_series(out, col)
            if s is not None:
                out[col] = s

    try:
        out = out.loc[:, ~out.columns.duplicated(keep="last")].copy()
    except Exception:
        pass

    return out


# ============================================================
# normalize OHLCV
# ============================================================

def normalize_ohlcv_columns(df: pd.DataFrame) -> pd.DataFrame:
    out = _ensure_dataframe(df)
    if out.empty:
        return out

    out = _coalesce_duplicate_labels(out)

    idx = out.index

    open_s = _coalesce_first_nonnull(out, _OPEN_ALIASES, numeric=True)
    high_s = _coalesce_first_nonnull(out, _HIGH_ALIASES, numeric=True)
    low_s = _coalesce_first_nonnull(out, _LOW_ALIASES, numeric=True)
    close_s = _coalesce_first_nonnull(out, _CLOSE_ALIASES, numeric=True)
    volume_s = _coalesce_first_nonnull(out, _VOLUME_ALIASES, numeric=True).fillna(0.0)

    # close を最終fallbackに使う
    open_s = open_s.where(open_s.notna(), close_s)
    high_s = high_s.where(high_s.notna(), close_s)
    low_s = low_s.where(low_s.notna(), close_s)

    out["open"] = open_s
    out["high"] = high_s
    out["low"] = low_s
    out["close"] = close_s
    out["volume"] = volume_s

    out["open_price"] = open_s
    out["high_price"] = high_s
    out["low_price"] = low_s
    out["close_price"] = close_s
    out["trading_volume"] = volume_s

    return out


# ============================================================
# normalize symbol / text
# ============================================================

def normalize_symbol_columns(df: pd.DataFrame) -> pd.DataFrame:
    out = _ensure_dataframe(df)
    if out.empty:
        return out

    idx = out.index

    symbol_s = _coalesce_first_nonnull(out, _SYMBOL_ALIASES, numeric=False)
    symbolname_s = _coalesce_first_nonnull(out, _SYMBOLNAME_ALIASES, numeric=False)

    try:
        symbol_s = symbol_s.astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
        symbol_s = symbol_s.replace({"nan": "", "None": "", "<NA>": ""})
    except Exception:
        pass

    try:
        symbolname_s = symbolname_s.astype(str).str.strip()
        symbolname_s = symbolname_s.replace({"nan": "", "None": "", "<NA>": ""})
    except Exception:
        pass

    out["symbol"] = symbol_s.reindex(idx).fillna("")
    out["symbolname"] = symbolname_s.reindex(idx).fillna("")

    miss = out["symbolname"].astype(str).str.strip().eq("")
    if miss.any():
        out.loc[miss, "symbolname"] = out.loc[miss, "symbol"]

    return out


# ============================================================
# normalize datetime
# ============================================================

def normalize_datetime_columns(df: pd.DataFrame) -> pd.DataFrame:
    out = _ensure_dataframe(df)
    if out.empty:
        return out

    idx = out.index
    dt_s = _coalesce_first_nonnull(out, _DATETIME_ALIASES, numeric=False)

    try:
        parsed = pd.to_datetime(dt_s, errors="coerce")
        try:
            if getattr(parsed.dt, "tz", None) is not None:
                parsed = parsed.dt.tz_localize(None)
        except Exception:
            pass
        out["datetime"] = parsed.reindex(idx)
    except Exception:
        out["datetime"] = pd.NaT

    return out


# ============================================================
# public orchestrator
# ============================================================

def normalize_scoring_input_columns(df: pd.DataFrame) -> pd.DataFrame:
    out = _ensure_dataframe(df)
    if out.empty:
        return out

    try:
        out.replace([np.inf, -np.inf], np.nan, inplace=True)
    except Exception:
        pass

    out = _coalesce_duplicate_labels(out)
    out = normalize_symbol_columns(out)
    out = normalize_datetime_columns(out)
    out = normalize_ohlcv_columns(out)

    # sort for downstream stability
    try:
        sort_cols = []
        if "symbol" in out.columns:
            sort_cols.append("symbol")
        if "datetime" in out.columns:
            sort_cols.append("datetime")
        if sort_cols:
            out = out.sort_values(sort_cols, kind="stable").reset_index(drop=True)
    except Exception:
        logger.debug("[SCORING PIPELINE] sort failed", exc_info=True)

    return out