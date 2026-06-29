# ============================================================
# File   : scripts/night_yahoo_pandas_safety_patch.py
# Version: V1-NIGHT-YAHOO-PANDAS-SAFETY
# ------------------------------------------------------------
# yfinance/pandasの返却形揺れで夜間Yahoo更新が全銘柄連続失敗する問題を抑止する。
# 対策:
#   - MultiIndex/重複カラムを正規化
#   - 空リスト pd.concat を空DataFrameにする
#   - enrich失敗時も最低限のDFを返して銘柄処理を継続
# ============================================================

from __future__ import annotations

import logging
from typing import Any

import pandas as pd

LOG = logging.getLogger("night_yahoo_pandas_safety_patch")
VERSION = "V1-NIGHT-YAHOO-PANDAS-SAFETY"
_INSTALLED = False
_ORIG_CONCAT = None
_PATCHED: set[int] = set()
_OHLCV_NAMES = {"open", "high", "low", "close", "adj_close", "adjclose", "volume", "date", "datetime"}


def _sanitize_columns(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return pd.DataFrame() if df is None else df
    out = df.copy()
    if isinstance(out.columns, pd.MultiIndex):
        pick = 0
        for i in range(out.columns.nlevels):
            vals = {str(x).strip().lower().replace(" ", "_") for x in out.columns.get_level_values(i)}
            if vals & _OHLCV_NAMES:
                pick = i
                break
        out.columns = out.columns.get_level_values(pick)
    out.columns = [str(c).strip() for c in out.columns]
    if pd.Index(out.columns).duplicated().any():
        out = out.loc[:, ~pd.Index(out.columns).duplicated(keep="first")].copy()
    return out


def _safe_concat(objs, *args, **kwargs):
    orig = _ORIG_CONCAT or pd.concat
    try:
        if isinstance(objs, (list, tuple)):
            cleaned = [x for x in objs if x is not None and not (hasattr(x, "empty") and x.empty)]
            if not cleaned:
                return pd.DataFrame()
            return orig(cleaned, *args, **kwargs)
        return orig(objs, *args, **kwargs)
    except ValueError as e:
        if "No objects to concatenate" in str(e):
            return pd.DataFrame()
        raise


def _fallback_standardize_daily(raw: pd.DataFrame, symbol: str, normalize_symbol) -> pd.DataFrame:
    if raw is None or not isinstance(raw, pd.DataFrame) or raw.empty:
        return pd.DataFrame()
    df = _sanitize_columns(raw).reset_index()
    if df.empty:
        return pd.DataFrame()
    dt_col = next((c for c in df.columns if str(c).lower() in {"date", "datetime", "index"}), df.columns[0])
    df["date"] = pd.to_datetime(df[dt_col], errors="coerce").dt.normalize()
    rename: dict[Any, str] = {}
    for c in df.columns:
        lc = str(c).strip().lower().replace(" ", "_")
        if lc == "open": rename[c] = "open"
        elif lc == "high": rename[c] = "high"
        elif lc == "low": rename[c] = "low"
        elif lc == "close": rename[c] = "close"
        elif lc in {"adj_close", "adjclose"}: rename[c] = "adj_close"
        elif lc == "volume": rename[c] = "volume"
    df = _sanitize_columns(df.rename(columns=rename))
    required = ["open", "high", "low", "close", "volume"]
    if any(c not in df.columns for c in required):
        return pd.DataFrame()
    for c in required + (["adj_close"] if "adj_close" in df.columns else []):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    if "adj_close" not in df.columns:
        df["adj_close"] = df["close"]
    df = df.dropna(subset=["date", "open", "high", "low", "close"]).copy()
    if df.empty:
        return pd.DataFrame()
    df["volume"] = df["volume"].fillna(0)
    df["symbol"] = normalize_symbol(symbol)
    df = df.drop_duplicates(subset=["symbol", "date"], keep="last").sort_values("date")
    return df[["symbol", "date", "open", "high", "low", "close", "adj_close", "volume"]].copy()


def _patch_daily_module(daily_mod: Any) -> None:
    if daily_mod is None or id(daily_mod) in _PATCHED:
        return
    normalize_symbol = getattr(daily_mod, "_normalize_symbol", lambda x: str(x or "").strip())
    original_standardize = getattr(daily_mod, "_standardize_daily", None)
    if callable(original_standardize):
        def standardize_daily_safe(raw: pd.DataFrame, symbol: str) -> pd.DataFrame:
            clean = _sanitize_columns(raw) if isinstance(raw, pd.DataFrame) else raw
            try:
                return original_standardize(clean, symbol)
            except Exception as e:
                LOG.warning("[NIGHT YAHOO PANDAS SAFETY] standardize retry symbol=%s err=%s", symbol, e)
                return _fallback_standardize_daily(raw, symbol, normalize_symbol)
        daily_mod._standardize_daily = standardize_daily_safe
    original_save = getattr(daily_mod, "_save_symbol_df", None)
    if callable(original_save):
        def save_symbol_df_safe(db_path, df: pd.DataFrame):
            return original_save(db_path, _sanitize_columns(df) if isinstance(df, pd.DataFrame) else df)
        daily_mod._save_symbol_df = save_symbol_df_safe
    _PATCHED.add(id(daily_mod))


def _patch_full_columns_module(fullcol_mod: Any) -> None:
    if fullcol_mod is None or id(fullcol_mod) in _PATCHED:
        return
    original_enrich = getattr(fullcol_mod, "enrich_daily_columns", None)
    if callable(original_enrich):
        def enrich_daily_columns_safe(df: pd.DataFrame) -> pd.DataFrame:
            clean = _sanitize_columns(df) if isinstance(df, pd.DataFrame) else df
            try:
                return original_enrich(clean)
            except Exception as e:
                LOG.warning("[NIGHT YAHOO PANDAS SAFETY] enrich fallback err=%s", e, exc_info=True)
                out = clean.copy() if isinstance(clean, pd.DataFrame) else pd.DataFrame()
                if out.empty:
                    return out
                if "date" in out.columns:
                    out["date"] = pd.to_datetime(out["date"], errors="coerce").dt.strftime("%Y-%m-%d")
                for col in getattr(fullcol_mod, "EXPECTED_COLS", []):
                    if col not in out.columns:
                        out[col] = pd.NA
                return out
        fullcol_mod.enrich_daily_columns = enrich_daily_columns_safe
    _PATCHED.add(id(fullcol_mod))


def install(daily_mod: Any = None, fullcol_mod: Any = None) -> bool:
    global _INSTALLED, _ORIG_CONCAT
    if not _INSTALLED:
        _ORIG_CONCAT = pd.concat
        pd.concat = _safe_concat
        _INSTALLED = True
        LOG.warning("[NIGHT YAHOO PANDAS SAFETY] installed version=%s", VERSION)
    _patch_daily_module(daily_mod)
    _patch_full_columns_module(fullcol_mod)
    return True
