# ============================================================
# File   : trading/summary/engine/incremental/sources.py
# Version: Ver1.0-INCREMENTAL-SOURCES
# ============================================================

from __future__ import annotations

import logging

import pandas as pd

from global_state import global_data
from utils.df_guard.core import sanitize
from utils.df_guard.symbol_guard import ensure_symbol
from utils.market_filter import filter_tradeable_dataframe

from .common import log_df_state, safe_log_error, to_dataframe, today_date

logger = logging.getLogger(__name__)


def coerce_push_minimal_columns(df_push: pd.DataFrame) -> pd.DataFrame:
    try:
        if df_push is None or not isinstance(df_push, pd.DataFrame) or df_push.empty:
            return pd.DataFrame()

        src = df_push.copy()

        colmap = {
            "symbol": ["symbol", "Symbol", "code", "Code", "symbol_code"],
            "datetime": [
                "datetime", "dt", "timestamp", "current_time",
                "CurrentPriceTime", "current_price_time", "received_at"
            ],
            "open": ["open", "open_price", "始値", "Open", "OpenPrice"],
            "high": ["high", "high_price", "高値", "High", "HighPrice"],
            "low": ["low", "low_price", "安値", "Low", "LowPrice"],
            "close": [
                "close", "close_price", "price", "current_price",
                "CurrentPrice", "last_price", "LastPrice", "終値",
                "Close", "ClosePrice"
            ],
            "volume": [
                "volume", "cum_volume", "出来高",
                "TradingVolume", "trading_volume", "last_cum_volume", "Volume"
            ],
            "vwap": ["vwap", "VWAP"],
            "symbolname": ["symbolname", "SymbolName", "symbol_name", "name", "Name"],
        }

        out = pd.DataFrame()

        for target, candidates in colmap.items():
            found = next((c for c in candidates if c in src.columns), None)
            if found is not None:
                out[target] = src[found]

        if out.empty:
            logger.warning(
                "[INCREMENTAL SUMMARY] minimal coerce found no usable columns source_cols=%s",
                list(src.columns)[:50],
            )
            return pd.DataFrame()

        if "symbol" in out.columns:
            out["symbol"] = out["symbol"].map(
                lambda x: x if isinstance(x, (str, int, float, type(None))) else None
            )
            out["symbol"] = out["symbol"].astype("string").str.strip().str.replace(r"\.0$", "", regex=True)

        if "datetime" in out.columns:
            out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
            try:
                out["datetime"] = out["datetime"].dt.tz_localize(None)
            except Exception:
                pass

        for c in ("open", "high", "low", "close", "volume", "vwap"):
            if c in out.columns:
                out[c] = pd.to_numeric(out[c], errors="coerce")

        if "close" in out.columns:
            if "open" not in out.columns:
                out["open"] = out["close"]
            if "high" not in out.columns:
                out["high"] = out["close"]
            if "low" not in out.columns:
                out["low"] = out["close"]

        if "symbolname" not in out.columns and "symbol" in out.columns:
            out["symbolname"] = out["symbol"]

        required = [c for c in ("symbol", "datetime", "close") if c in out.columns]
        if required:
            out = out.dropna(subset=required)

        if out.empty:
            return pd.DataFrame()

        out = out.sort_values(["symbol", "datetime"], kind="stable")
        out = out.drop_duplicates(subset=["symbol", "datetime"], keep="last")
        return out.reset_index(drop=True)

    except Exception as e:
        safe_log_error("[INCREMENTAL SUMMARY] coerce push minimal columns failed", exc=e)
        return pd.DataFrame()


def read_candidate_df(candidate_name: str) -> pd.DataFrame:
    try:
        obj = getattr(global_data, candidate_name, None)
    except Exception as e:
        safe_log_error("[INCREMENTAL SUMMARY] getattr failed candidate=%s", candidate_name, exc=e)
        return pd.DataFrame()

    try:
        if callable(obj):
            obj = obj()
    except Exception as e:
        safe_log_error("[INCREMENTAL SUMMARY] callable candidate failed=%s", candidate_name, exc=e)
        return pd.DataFrame()

    df = to_dataframe(obj)
    if not df.empty:
        logger.info(
            "[INCREMENTAL SUMMARY] runtime push candidate hit=%s rows=%s cols=%s",
            candidate_name,
            len(df),
            len(df.columns),
        )
    return df


def read_push_via_method() -> pd.DataFrame:
    try:
        getter = getattr(global_data, "get_push_df", None)
    except Exception as e:
        safe_log_error("[INCREMENTAL SUMMARY] resolve get_push_df failed", exc=e)
        return pd.DataFrame()

    if not callable(getter):
        return pd.DataFrame()

    try:
        obj = getter()
        df = to_dataframe(obj)
        if not df.empty:
            logger.info(
                "[INCREMENTAL SUMMARY] runtime push via get_push_df rows=%s cols=%s",
                len(df),
                len(df.columns),
            )
        else:
            logger.warning("[INCREMENTAL SUMMARY] get_push_df returned empty")
        return df
    except Exception as e:
        safe_log_error("[INCREMENTAL SUMMARY] get_push_df failed", exc=e)
        return pd.DataFrame()


def get_runtime_push_raw_df() -> pd.DataFrame:
    candidates = [
        "push_df",
        "stream_data",
        "latest_push_df",
        "push_snapshot_df",
        "push_data",
    ]

    df = read_push_via_method()
    if not df.empty:
        return df

    for name in candidates:
        df = read_candidate_df(name)
        if not df.empty:
            return df

    logger.warning("[INCREMENTAL SUMMARY] no runtime push dataframe candidates available")
    return pd.DataFrame()


def get_push_base_df() -> pd.DataFrame:
    try:
        df_push_raw = get_runtime_push_raw_df()
        if df_push_raw.empty:
            logger.warning("[INCREMENTAL SUMMARY] runtime push raw df empty")
            return pd.DataFrame()

        logger.info(
            "[INCREMENTAL SUMMARY] runtime push raw rows=%s cols=%s sample_cols=%s",
            len(df_push_raw),
            len(df_push_raw.columns),
            list(df_push_raw.columns)[:30],
        )

        df_push = coerce_push_minimal_columns(df_push_raw)
        if df_push.empty:
            logger.warning("[INCREMENTAL SUMMARY] push_df empty after minimal coerce")
            return pd.DataFrame()

        try:
            df_push = sanitize(df_push, mode="light")
        except Exception as e:
            safe_log_error("[INCREMENTAL SUMMARY] sanitize failed but continue", exc=e)

        try:
            df_push = ensure_symbol(df_push)
        except Exception as e:
            safe_log_error("[INCREMENTAL SUMMARY] ensure_symbol failed but continue", exc=e)

        before_filter = df_push.copy()

        try:
            df_push = filter_tradeable_dataframe(df_push)
        except Exception as e:
            safe_log_error("[INCREMENTAL SUMMARY] market filter failed but continue", exc=e)

        if df_push.empty and not before_filter.empty:
            logger.warning(
                "[INCREMENTAL SUMMARY] market filter dropped all rows -> fallback to unfiltered push rows=%s",
                len(before_filter),
            )
            df_push = before_filter

        if df_push.empty:
            logger.warning("[INCREMENTAL SUMMARY] push_df empty after sanitize/filter")
            return pd.DataFrame()

        if "symbol" in df_push.columns:
            df_push["symbol"] = df_push["symbol"].astype(str)

        if "datetime" in df_push.columns:
            df_push["datetime"] = pd.to_datetime(df_push["datetime"], errors="coerce")
            try:
                df_push["datetime"] = df_push["datetime"].dt.tz_localize(None)
            except Exception:
                pass

        required = [c for c in ("symbol", "datetime", "close") if c in df_push.columns]
        if required:
            df_push = df_push.dropna(subset=required)

        if df_push.empty:
            logger.warning("[INCREMENTAL SUMMARY] push_df empty after final required drop")
            return pd.DataFrame()

        if "datetime" in df_push.columns:
            before = len(df_push)
            df_push = df_push.loc[df_push["datetime"].dt.date == today_date()].copy()
            logger.info(
                "[INCREMENTAL SUMMARY] push today filter rows=%s -> %s today=%s",
                before,
                len(df_push),
                today_date(),
            )

        if df_push.empty:
            logger.warning("[INCREMENTAL SUMMARY] push_df empty after today filter")
            return pd.DataFrame()

        df_push = (
            df_push
            .sort_values(["symbol", "datetime"], kind="stable")
            .drop_duplicates(subset=["symbol", "datetime"], keep="last")
            .reset_index(drop=True)
        )

        log_df_state("push-input", df_push)
        return df_push

    except Exception as e:
        safe_log_error("[INCREMENTAL SUMMARY] push base normalize failed", exc=e)
        return pd.DataFrame()