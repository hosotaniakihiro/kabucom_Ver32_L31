# ============================================================
# File   : trading/summary/persistence/dataframe_utils.py
# Version: Ver1.0-PRODUCTION-DATAFRAME-UTILS
# ------------------------------------------------------------
# 機能:
# - DataFrame安全変換
# - Series安全取得
# - 重複列のcoalesce
# - symbol列の推定/正規化
# - OHLC別名列の補正
# - 数値列のcoalesce
# - datetime naive変換
# ------------------------------------------------------------
# 主な責務:
# - 保存前の基礎的な表形式整形
# - symbol / OHLC / duplicate columns の吸収
# ============================================================

from __future__ import annotations

import logging
from typing import Optional

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
            logger.exception("[SUMMARY] dataframe conversion failed")
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
        logger.exception("[SUMMARY] safe_get_series failed col=%s", col)
        return None


def coalesce_duplicate_columns(df: pd.DataFrame) -> pd.DataFrame:
    df = ensure_dataframe(df)
    if df.empty:
        return df

    try:
        cols = list(df.columns)
        if len(cols) == len(set(cols)):
            return df
    except Exception:
        return df

    try:
        unique_cols = []
        seen = set()
        for c in df.columns:
            if c not in seen:
                unique_cols.append(c)
                seen.add(c)

        out = {}
        for c in unique_cols:
            idxs = [i for i, name in enumerate(df.columns) if name == c]
            if len(idxs) == 1:
                out[c] = df.iloc[:, idxs[0]]
            else:
                s = df.iloc[:, idxs[0]]
                for j in idxs[1:]:
                    try:
                        s = s.combine_first(df.iloc[:, j])
                    except Exception:
                        try:
                            s = s.where(s.notna(), df.iloc[:, j])
                        except Exception:
                            pass
                out[c] = s

        return pd.DataFrame(out).reset_index(drop=True)
    except Exception:
        logger.exception("[SUMMARY] duplicate column coalesce failed")
        try:
            return df.loc[:, ~df.columns.duplicated()].copy()
        except Exception:
            return df


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
            logger.debug(
                "[SUMMARY] numeric coalesce failed dest=%s src=%s",
                dest,
                c,
                exc_info=True,
            )

    out[dest] = base
    return out


def normalize_symbol(df: pd.DataFrame) -> pd.DataFrame:
    df = ensure_dataframe(df)
    if df.empty:
        return df

    out = df.copy()

    try:
        if "symbol" not in out.columns and getattr(out.index, "name", None) == "symbol":
            out = out.reset_index()
    except Exception:
        logger.debug("[SUMMARY] reset_index named symbol failed", exc_info=True)

    try:
        if "symbol" not in out.columns and not isinstance(out.index, pd.RangeIndex):
            out = out.reset_index()
    except Exception:
        logger.debug("[SUMMARY] reset_index generic failed", exc_info=True)

    symbol_candidates = [
        "symbol", "Symbol", "SYMBOL",
        "code", "Code", "CODE",
        "ticker", "Ticker", "TICKER",
        "stock_code", "銘柄コード",
        "symbol_x", "symbol_y",
        "Symbol_x", "Symbol_y",
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
                logger.debug("[SUMMARY] symbol normalize failed col=%s", col, exc_info=True)

    if "symbol" not in out.columns:
        try:
            for c in out.columns:
                s = safe_get_series(out, c)
                if s is not None and looks_like_symbol_series(s):
                    out["symbol"] = cleanup_symbol_series(s)
                    logger.info("[SUMMARY] symbol rescued from column=%s", c)
                    break
        except Exception:
            logger.debug("[SUMMARY] symbol rescue scan failed", exc_info=True)

    if "symbol" in out.columns:
        try:
            out["symbol"] = cleanup_symbol_series(out["symbol"])
        except Exception:
            logger.debug("[SUMMARY] symbol cleanup failed", exc_info=True)

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
        "openPrice": "open",
        "highPrice": "high",
        "lowPrice": "low",
        "closePrice": "close",
        "OpenPrice": "open",
        "HighPrice": "high",
        "LowPrice": "low",
        "ClosePrice": "close",
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
        "close", "close_price", "price",
        "current_price", "CurrentPrice", "currentPrice",
        "last_price", "LastPrice", "last",
        "currentvalue", "value",
        "closePrice", "ClosePrice",
    ])
    out = coalesce_first_numeric(out, "open", [
        "open", "open_price", "openPrice", "OpenPrice",
        "opening_price", "OpeningPrice", "opening",
        "price", "current_price", "CurrentPrice", "currentPrice",
        "close", "close_price",
    ])
    out = coalesce_first_numeric(out, "high", [
        "high", "high_price", "highPrice", "HighPrice",
        "price", "current_price", "CurrentPrice", "currentPrice",
        "close", "close_price",
    ])
    out = coalesce_first_numeric(out, "low", [
        "low", "low_price", "lowPrice", "LowPrice",
        "price", "current_price", "CurrentPrice", "currentPrice",
        "close", "close_price",
    ])
    out = coalesce_first_numeric(out, "volume", [
        "volume", "Volume", "trading_volume", "TradingVolume", "qty", "total_volume",
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
        logger.debug("[SUMMARY] ohlc backfill from close failed", exc_info=True)

    alias_map = {
        "open_price": "open",
        "high_price": "high",
        "low_price": "low",
        "close_price": "close",
        "price": "close",
        "current_price": "close",
        "CurrentPrice": "close",
        "last_price": "close",
        "LastPrice": "close",
        "trading_volume": "volume",
        "TradingVolume": "volume",
    }

    for alias, src in alias_map.items():
        try:
            if alias not in out.columns:
                out[alias] = pd.to_numeric(safe_get_series(out, src), errors="coerce")
            else:
                existing = pd.to_numeric(safe_get_series(out, alias), errors="coerce")
                src_s = pd.to_numeric(safe_get_series(out, src), errors="coerce")
                out[alias] = existing.combine_first(src_s)
        except Exception:
            logger.debug(
                "[SUMMARY] alias sync failed alias=%s src=%s",
                alias,
                src,
                exc_info=True,
            )

    return out