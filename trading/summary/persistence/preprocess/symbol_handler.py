# -*- coding: utf-8 -*-
from __future__ import annotations

import logging
from typing import Iterable, Optional

import pandas as pd

logger = logging.getLogger(__name__)

_SYMBOL_ALIASES = [
    "symbol",
    "Symbol",
    "SYMBOL",
    "code",
    "Code",
    "CODE",
    "ticker",
    "Ticker",
    "TICKER",
    "stock_code",
    "銘柄コード",
    "銘柄ｺｰﾄﾞ",
    "証券コード",
]

_SYMBOL_MERGE_ALIASES = [
    "symbol_x",
    "symbol_y",
    "Symbol_x",
    "Symbol_y",
    "code_x",
    "code_y",
    "ticker_x",
    "ticker_y",
]

_SYMBOLNAME_ALIASES = [
    "symbolname",
    "SymbolName",
    "SYMBOLNAME",
    "name",
    "Name",
    "銘柄名",
    "銘柄名称",
]


def _first_existing(columns: Iterable[str], candidates: Iterable[str]) -> Optional[str]:
    cols = set(columns)
    for c in candidates:
        if c in cols:
            return c
    return None


def _normalize_symbol_series(sr: pd.Series) -> pd.Series:
    out = sr.astype(str).str.strip()
    out = out.str.replace(".0", "", regex=False)
    out = out.replace(
        {
            "": pd.NA,
            "nan": pd.NA,
            "None": pd.NA,
            "null": pd.NA,
            "<NA>": pd.NA,
        }
    )
    return out


def _ensure_symbol_present(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame() if df is None else df

    out = df.copy()

    if "symbol" not in out.columns:
        for c in (
            "Symbol",
            "SYMBOL",
            "code",
            "Code",
            "ticker",
            "Ticker",
            "stock_code",
            "symbol_x",
            "symbol_y",
            "Symbol_x",
            "Symbol_y",
            "銘柄コード",
        ):
            if c in out.columns:
                try:
                    out["symbol"] = (
                        out[c]
                        .astype(str)
                        .str.strip()
                        .str.replace(r"\.0$", "", regex=True)
                    )
                    break
                except Exception:
                    logger.debug("[UPSERT] ensure_symbol_present failed src=%s", c, exc_info=True)

    if "symbol" in out.columns:
        try:
            out["symbol"] = out["symbol"].replace(
                {"": pd.NA, "nan": pd.NA, "None": pd.NA, "null": pd.NA, "<NA>": pd.NA}
            )
        except Exception:
            logger.debug("[UPSERT] symbol cleanup failed", exc_info=True)

    return out

def ensure_symbolname(df: pd.DataFrame) -> pd.DataFrame:
    """
    symbolname 列を復元する。
    既存コードとの import 互換のため公開する。
    """
    if df is None:
        logger.warning("[SYMBOL] ensure_symbolname input df is None")
        return df

    if not isinstance(df, pd.DataFrame):
        logger.warning("[SYMBOL] ensure_symbolname input is not DataFrame type=%s", type(df))
        return df

    if df.empty:
        if "symbolname" not in df.columns:
            try:
                df = df.copy()
                df["symbolname"] = pd.Series(dtype="object")
            except Exception:
                pass
        return df

    out = df.copy()

    if "symbolname" not in out.columns:
        src = _first_existing(out.columns, _SYMBOLNAME_ALIASES)
        if src:
            out["symbolname"] = out[src]
            logger.info("[SYMBOL] restored symbolname from alias=%s", src)
        else:
            out["symbolname"] = ""

    try:
        out["symbolname"] = out["symbolname"].fillna("").astype(str).str.strip()
    except Exception:
        logger.debug("[SYMBOL] symbolname normalize failed", exc_info=True)

    return out


def ensure_datetime_column(df: pd.DataFrame) -> pd.DataFrame:
    """
    datetime が無い場合に start_time/end_time/time/tick_time から復元する。
    """
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return df

    out = df.copy()

    if "datetime" in out.columns:
        out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
        return out

    for candidate in ("end_time", "start_time", "time", "tick_time", "snapshot_time"):
        if candidate in out.columns:
            out["datetime"] = pd.to_datetime(out[candidate], errors="coerce")
            logger.info("[SYMBOL] restored datetime from %s", candidate)
            return out

    logger.warning("[SYMBOL] datetime column missing and no fallback source found")
    return out


def ensure_date_time_range(df: pd.DataFrame, interval: int) -> pd.DataFrame:
    """
    DB UNIQUE(symbol, date, time_range) 用の列を補完する。
    time_range 形式: HH:MM-HH:MM
    """
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return df

    out = ensure_datetime_column(df)
    if out is None or out.empty:
        return out

    if "datetime" not in out.columns:
        logger.warning("[SYMBOL] cannot ensure date/time_range because datetime missing")
        return out

    dtv = pd.to_datetime(out["datetime"], errors="coerce")

    if "date" not in out.columns:
        out["date"] = dtv.dt.strftime("%Y-%m-%d")
        logger.info("[SYMBOL] generated date from datetime")
    else:
        try:
            out["date"] = pd.to_datetime(out["date"], errors="coerce").dt.strftime("%Y-%m-%d")
        except Exception:
            logger.debug("[SYMBOL] date normalize failed", exc_info=True)

    if "time_range" not in out.columns:
        try:
            start = dtv.dt.floor(f"{int(interval)}min")
            end = start + pd.to_timedelta(int(interval) - 1, unit="min")
            out["time_range"] = (
                start.dt.strftime("%H:%M") + "-" + end.dt.strftime("%H:%M")
            )
            logger.info("[SYMBOL] generated time_range interval=%s", interval)
        except Exception:
            logger.exception("[SYMBOL] failed to generate time_range interval=%s", interval)
    else:
        try:
            sr = out["time_range"].astype(str)
            need_fill = sr.isna() | (sr.str.strip() == "") | sr.isin(
                ["1min", "3min", "5min", "10min", "15min", "30min", "60min"]
            )
            if need_fill.any():
                start = dtv.dt.floor(f"{int(interval)}min")
                end = start + pd.to_timedelta(int(interval) - 1, unit="min")
                built = start.dt.strftime("%H:%M") + "-" + end.dt.strftime("%H:%M")
                out.loc[need_fill, "time_range"] = built.loc[need_fill]
        except Exception:
            logger.debug("[SYMBOL] time_range normalize failed", exc_info=True)

    return out


def preprocess_summary_keys(df: pd.DataFrame, interval: int) -> pd.DataFrame:
    """
    保存前に最低限必要なキー列を揃える。
    """
    if df is None or not isinstance(df, pd.DataFrame):
        return df

    out = df.copy()
    out = ensure_symbol_column(out)
    out = ensure_symbolname(out)
    out = ensure_datetime_column(out)
    out = ensure_date_time_range(out, interval=interval)

    key_cols = ["symbol", "symbolname", "date", "time_range", "datetime"]
    for c in key_cols:
        if c not in out.columns:
            logger.warning("[SYMBOL] key column still missing: %s", c)

    return out


__all__ = [
    "ensure_symbol_column",
    "ensure_symbolname",
    "ensure_datetime_column",
    "ensure_date_time_range",
    "preprocess_summary_keys",
]