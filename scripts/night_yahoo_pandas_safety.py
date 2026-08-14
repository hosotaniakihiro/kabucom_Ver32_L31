# ============================================================
# File   : scripts/night_yahoo_pandas_safety.py
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

LOG = logging.getLogger("night_yahoo_pandas_safety")
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

def install_concat_guard() -> bool:
    """pd.concat を空リスト安全版へ差し替える。

    旧 install() の唯一のグローバル副作用。モジュール単位のラッパー
    (_standardize_daily / _save_symbol_df / enrich_daily_columns) は
    それぞれの本体へ本文化済み。
    """
    global _INSTALLED, _ORIG_CONCAT
    if _INSTALLED:
        return True
    _ORIG_CONCAT = pd.concat
    pd.concat = _safe_concat
    _INSTALLED = True
    LOG.warning("[NIGHT YAHOO PANDAS SAFETY] installed version=%s", VERSION)
    return True


sanitize_columns = _sanitize_columns
fallback_standardize_daily = _fallback_standardize_daily

__all__ = [
    "VERSION",
    "install_concat_guard",
    "sanitize_columns",
    "fallback_standardize_daily",
]

