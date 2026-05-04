# ============================================================
# File   : trading/yahoo/ranking_follow/df_cache.py
# Version: PRODUCTION-STABLE-YAHOO-RANKING-FOLLOW-DFCACHE-REV1.0
# ------------------------------------------------------------
# Purpose:
#   Yahoo 1分足・Yahoo由来サマリーの DataFrame をメモリ上で
#   ロック付き管理する。
#
# Guarantees:
#   - symbol + datetime で重複排除
#   - 既存DFへ差分だけ merge/upsert
#   - 外部へ返すときは copy を返し、破壊的変更を防ぐ
#   - 1m / 3m / 5m を別々に保持
# ============================================================

from __future__ import annotations

import logging
import threading
from typing import Dict, Optional

import pandas as pd

logger = logging.getLogger(__name__)

_LOCK = threading.RLock()
_RAW_1M_DF = pd.DataFrame()
_SUMMARY_DF: Dict[int, pd.DataFrame] = {1: pd.DataFrame(), 3: pd.DataFrame(), 5: pd.DataFrame()}

_KEY_COLS = ["symbol", "datetime"]


def _normalize_df(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    out = df.copy()

    if "symbol" in out.columns:
        out["symbol"] = out["symbol"].astype(str).str.replace(r"\.0$", "", regex=True).str.strip()

    if "datetime" in out.columns:
        out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
        out = out[out["datetime"].notna()].copy()

    if "date" not in out.columns and "datetime" in out.columns:
        out["date"] = out["datetime"].dt.strftime("%Y-%m-%d")
    if "time" not in out.columns and "datetime" in out.columns:
        out["time"] = out["datetime"].dt.strftime("%H:%M:%S")

    if all(c in out.columns for c in _KEY_COLS):
        out = out.sort_values(_KEY_COLS).drop_duplicates(_KEY_COLS, keep="last")

    return out.reset_index(drop=True)


def _merge_by_key(base: pd.DataFrame, delta: pd.DataFrame) -> pd.DataFrame:
    delta = _normalize_df(delta)
    if delta.empty:
        return _normalize_df(base)
    if base is None or base.empty:
        return delta

    base = _normalize_df(base)
    if base.empty:
        return delta

    missing_cols = [c for c in delta.columns if c not in base.columns]
    for c in missing_cols:
        base[c] = pd.NA
    missing_cols = [c for c in base.columns if c not in delta.columns]
    for c in missing_cols:
        delta[c] = pd.NA

    cols = list(base.columns)
    merged = pd.concat([base[cols], delta[cols]], ignore_index=True)
    if all(c in merged.columns for c in _KEY_COLS):
        merged = merged.sort_values(_KEY_COLS).drop_duplicates(_KEY_COLS, keep="last")
    return merged.reset_index(drop=True)


def merge_raw_1m(delta_df: pd.DataFrame) -> pd.DataFrame:
    global _RAW_1M_DF
    with _LOCK:
        before = len(_RAW_1M_DF)
        _RAW_1M_DF = _merge_by_key(_RAW_1M_DF, delta_df)
        after = len(_RAW_1M_DF)
        logger.info("[YAHOO DF CACHE] raw_1m merge before=%s delta=%s after=%s", before, len(delta_df) if delta_df is not None else 0, after)
        return _RAW_1M_DF.copy()


def set_raw_1m(df: pd.DataFrame) -> pd.DataFrame:
    global _RAW_1M_DF
    with _LOCK:
        _RAW_1M_DF = _normalize_df(df)
        logger.info("[YAHOO DF CACHE] raw_1m set rows=%s", len(_RAW_1M_DF))
        return _RAW_1M_DF.copy()


def get_raw_1m(symbol: Optional[str] = None) -> pd.DataFrame:
    with _LOCK:
        df = _RAW_1M_DF.copy()
    if symbol and not df.empty and "symbol" in df.columns:
        return df[df["symbol"].astype(str) == str(symbol)].copy()
    return df


def merge_summary(interval: int, delta_df: pd.DataFrame) -> pd.DataFrame:
    interval = int(interval)
    if interval not in _SUMMARY_DF:
        _SUMMARY_DF[interval] = pd.DataFrame()
    with _LOCK:
        before = len(_SUMMARY_DF[interval])
        _SUMMARY_DF[interval] = _merge_by_key(_SUMMARY_DF[interval], delta_df)
        after = len(_SUMMARY_DF[interval])
        logger.info(
            "[YAHOO DF CACHE] summary merge interval=%s before=%s delta=%s after=%s",
            interval, before, len(delta_df) if delta_df is not None else 0, after,
        )
        return _SUMMARY_DF[interval].copy()


def set_summary(interval: int, df: pd.DataFrame) -> pd.DataFrame:
    interval = int(interval)
    with _LOCK:
        _SUMMARY_DF[interval] = _normalize_df(df)
        logger.info("[YAHOO DF CACHE] summary set interval=%s rows=%s", interval, len(_SUMMARY_DF[interval]))
        return _SUMMARY_DF[interval].copy()


def get_summary(interval: int, symbol: Optional[str] = None) -> pd.DataFrame:
    interval = int(interval)
    with _LOCK:
        df = _SUMMARY_DF.get(interval, pd.DataFrame()).copy()
    if symbol and not df.empty and "symbol" in df.columns:
        return df[df["symbol"].astype(str) == str(symbol)].copy()
    return df


def clear_all() -> None:
    global _RAW_1M_DF, _SUMMARY_DF
    with _LOCK:
        _RAW_1M_DF = pd.DataFrame()
        _SUMMARY_DF = {1: pd.DataFrame(), 3: pd.DataFrame(), 5: pd.DataFrame()}
        logger.info("[YAHOO DF CACHE] cleared")
