# ============================================================
# File   : trading/yahoo/pipeline/complement/resample.py
# Version: PRODUCTION-STABLE-REV4.1.1-YAHOO-COMPLEMENT-RESAMPLE-FIX
# ------------------------------------------------------------
# 【概要】
#   Yahoo 1分足から 3分足/5分足を生成する
#
# 【主な機能】
#   - 1分足はそのまま返す
#   - 3分足/5分足を 00分起点でリサンプル
#   - symbolごとにOHLCV集計
#   - label=right / closed=right
#   - symbolname維持
#   - symbol+datetime重複除去
#   - build_interval_frame を公開
#
# 【今回の修正】
#   - complement/__init__.py から import される
#       build_interval_frame
#     を確実に定義
#   - 互換用 alias:
#       build_yahoo_interval_frame
#       resample_1m_to_interval
#     も公開
#
# 【重要】
#   - 3分足: 09:00, 09:03, 09:06 ...
#   - 5分足: 09:00, 09:05, 09:10 ...
#   - datetime は resample の右端時刻として扱う
# ============================================================

from __future__ import annotations

import logging
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# optional normalize imports
# ============================================================

try:
    from .normalize import (
        safe_df,
        normalize_datetime_df,
        backfill_symbolname,
    )
except Exception:  # pragma: no cover
    def safe_df(df: Any) -> pd.DataFrame:
        try:
            if df is None:
                return pd.DataFrame()
            if isinstance(df, pd.DataFrame):
                out = df.copy()
            else:
                out = pd.DataFrame(df)

            if out.empty:
                return pd.DataFrame()

            try:
                out = out.loc[:, ~out.columns.duplicated()]
            except Exception:
                pass

            return out
        except Exception:
            return pd.DataFrame()

    def normalize_datetime_df(df: pd.DataFrame) -> pd.DataFrame:
        out = safe_df(df)
        if out.empty:
            return out
        if "datetime" not in out.columns:
            return pd.DataFrame()

        out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
        out = out.dropna(subset=["datetime"])

        if out.empty:
            return pd.DataFrame()

        try:
            if getattr(out["datetime"].dt, "tz", None) is not None:
                try:
                    out["datetime"] = out["datetime"].dt.tz_convert(None)
                except Exception:
                    out["datetime"] = out["datetime"].dt.tz_localize(None)
        except Exception:
            pass

        out["datetime"] = out["datetime"].dt.floor("min")
        return out

    def backfill_symbolname(df: pd.DataFrame) -> pd.DataFrame:
        out = safe_df(df)
        if out.empty:
            return out
        if "symbol" in out.columns and "symbolname" not in out.columns:
            out["symbolname"] = out["symbol"].astype(str)
        elif "symbol" in out.columns and "symbolname" in out.columns:
            s = out["symbolname"].fillna("").astype(str).str.strip()
            m = s.eq("") | s.isin(["nan", "None", "<NA>", "0", "0.0"])
            out["symbolname"] = s
            out.loc[m, "symbolname"] = out.loc[m, "symbol"].astype(str)
        return out


# ============================================================
# helpers
# ============================================================

def _normalize_symbol_value(v: Any) -> str:
    try:
        if pd.isna(v):
            return ""
        s = str(v).strip()
        s = s.replace(".T", "")
        if s.endswith(".0"):
            s2 = s[:-2]
            if s2.isdigit():
                return s2
        return s
    except Exception:
        return ""


def _ensure_ohlcv_columns(df: pd.DataFrame) -> pd.DataFrame:
    out = safe_df(df)
    if out.empty:
        return out

    alias_map = {
        "open_price": "open",
        "high_price": "high",
        "low_price": "low",
        "close_price": "close",
        "price": "close",
        "current_price": "close",
        "currentprice": "close",
        "CurrentPrice": "close",
        "last_price": "close",
        "lastprice": "close",
        "LastPrice": "close",
        "trading_volume": "volume",
        "tradingvolume": "volume",
        "TradingVolume": "volume",
    }

    for src, dst in alias_map.items():
        if src in out.columns and dst not in out.columns:
            out[dst] = out[src]

    if "volume" not in out.columns:
        out["volume"] = 0.0

    required = ["symbol", "datetime", "open", "high", "low", "close", "volume"]
    missing = [c for c in required if c not in out.columns]
    if missing:
        logger.warning(
            "[YAHOO RESAMPLE] missing required columns missing=%s actual=%s",
            missing,
            list(out.columns),
        )
        return pd.DataFrame()

    out["symbol"] = out["symbol"].map(_normalize_symbol_value)
    out = out[out["symbol"] != ""].copy()

    for c in ["open", "high", "low", "close", "volume"]:
        out[c] = pd.to_numeric(out[c], errors="coerce")

    # open/high/low が欠ける場合は close で補完
    for c in ["open", "high", "low"]:
        out[c] = out[c].fillna(out["close"])

    out["volume"] = out["volume"].fillna(0.0)

    out = out.dropna(subset=["symbol", "datetime", "open", "high", "low", "close"])
    if out.empty:
        return pd.DataFrame()

    out = backfill_symbolname(out)

    return out


def _add_summary_basic_columns(df: pd.DataFrame, interval: int) -> pd.DataFrame:
    out = safe_df(df)
    if out.empty:
        return out

    out = normalize_datetime_df(out)
    if out.empty:
        return out

    interval = int(interval)

    out["date"] = out["datetime"].dt.strftime("%Y-%m-%d")
    out["time"] = out["datetime"].dt.strftime("%H:%M:%S")

    out["start_time"] = (
        out["datetime"] - pd.to_timedelta(max(interval - 1, 0), unit="min")
    ).dt.strftime("%H:%M:%S")
    out["end_time"] = out["datetime"].dt.strftime("%H:%M:%S")

    start_hm = (
        out["datetime"] - pd.to_timedelta(max(interval - 1, 0), unit="min")
    ).dt.strftime("%H:%M")
    end_hm = out["datetime"].dt.strftime("%H:%M")
    out["time_range"] = start_hm + "-" + end_hm

    out["interval"] = interval

    out["open_price"] = out["open"]
    out["high_price"] = out["high"]
    out["low_price"] = out["low"]
    out["close_price"] = out["close"]

    out["price"] = out["close"]
    out["current_price"] = out["close"]
    out["trading_volume"] = out["volume"]

    out = backfill_symbolname(out)

    return out


# ============================================================
# resample core
# ============================================================

def resample_symbol_ohlcv(g: pd.DataFrame, interval: int) -> pd.DataFrame:
    """
    1銘柄分の1分足を指定intervalへリサンプルする。
    """
    try:
        interval = int(interval)

        work = safe_df(g)
        if work.empty:
            return pd.DataFrame()

        work = normalize_datetime_df(work)
        if work.empty:
            return pd.DataFrame()

        work = _ensure_ohlcv_columns(work)
        if work.empty:
            return pd.DataFrame()

        if interval == 1:
            out = work.copy()
            out = _add_summary_basic_columns(out, interval=1)
            out = (
                out.sort_values(["symbol", "datetime"], kind="stable")
                   .drop_duplicates(subset=["symbol", "datetime"], keep="last")
                   .reset_index(drop=True)
            )
            return out

        work = work.sort_values("datetime").reset_index(drop=True)

        symbol = ""
        if "symbol" in work.columns and not work["symbol"].empty:
            symbol = str(work["symbol"].iloc[0]).strip()

        symbolname = ""
        if "symbolname" in work.columns and not work["symbolname"].empty:
            nonempty = work["symbolname"].fillna("").astype(str).str.strip()
            nonempty = nonempty[nonempty != ""]
            if not nonempty.empty:
                symbolname = str(nonempty.iloc[-1]).strip()

        rs = work.set_index("datetime").resample(
            f"{interval}min",
            label="right",
            closed="right",
            origin="start_day",
            offset="0min",
        )

        out = pd.DataFrame(
            {
                "open": rs["open"].first(),
                "high": rs["high"].max(),
                "low": rs["low"].min(),
                "close": rs["close"].last(),
                "volume": rs["volume"].sum(min_count=1),
            }
        ).reset_index()

        out["symbol"] = symbol
        out["symbolname"] = symbolname

        out = out.dropna(subset=["datetime", "open", "high", "low", "close"])
        if out.empty:
            return pd.DataFrame()

        out["volume"] = pd.to_numeric(out["volume"], errors="coerce").fillna(0.0)

        out = _add_summary_basic_columns(out, interval=interval)

        out = (
            out.sort_values(["symbol", "datetime"], kind="stable")
               .drop_duplicates(subset=["symbol", "datetime"], keep="last")
               .reset_index(drop=True)
        )

        return out

    except Exception:
        logger.exception("[YAHOO RESAMPLE] resample_symbol_ohlcv failed interval=%s", interval)
        return pd.DataFrame()


def build_interval_frame(df_1min: pd.DataFrame, interval: int) -> pd.DataFrame:
    """
    Yahoo 1分足DataFrameから指定intervalのOHLCV frameを作る。

    Parameters
    ----------
    df_1min:
        正規化済み、またはYahoo由来1分足DataFrame

    interval:
        1, 3, 5 など

    Returns
    -------
    pd.DataFrame
        symbol, datetime, open, high, low, close, volume などを持つDataFrame
    """
    try:
        interval = int(interval)

        base = safe_df(df_1min)
        if base.empty:
            return pd.DataFrame()

        base = normalize_datetime_df(base)
        if base.empty:
            return pd.DataFrame()

        base = _ensure_ohlcv_columns(base)
        if base.empty:
            return pd.DataFrame()

        if interval == 1:
            out = base.copy()
            out = _add_summary_basic_columns(out, interval=1)
            out = (
                out.sort_values(["symbol", "datetime"], kind="stable")
                   .drop_duplicates(subset=["symbol", "datetime"], keep="last")
                   .reset_index(drop=True)
            )
            out = backfill_symbolname(out)

            logger.info(
                "[YAHOO RESAMPLE] built interval=1 rows=%s symbols=%s dt_min=%s dt_max=%s",
                len(out),
                out["symbol"].nunique() if "symbol" in out.columns else 0,
                out["datetime"].min() if "datetime" in out.columns and not out.empty else None,
                out["datetime"].max() if "datetime" in out.columns and not out.empty else None,
            )
            return out

        chunks: list[pd.DataFrame] = []

        for _, g in base.groupby("symbol", sort=False):
            one = resample_symbol_ohlcv(g, interval=interval)
            if one is not None and not one.empty:
                chunks.append(one)

        if not chunks:
            return pd.DataFrame()

        out = pd.concat(chunks, ignore_index=True)
        out = normalize_datetime_df(out)
        if out.empty:
            return pd.DataFrame()

        out = backfill_symbolname(out)

        out = (
            out.sort_values(["symbol", "datetime"], kind="stable")
               .drop_duplicates(subset=["symbol", "datetime"], keep="last")
               .reset_index(drop=True)
        )

        logger.info(
            "[YAHOO RESAMPLE] built interval=%s rows=%s symbols=%s dt_min=%s dt_max=%s",
            interval,
            len(out),
            out["symbol"].nunique() if "symbol" in out.columns else 0,
            out["datetime"].min() if "datetime" in out.columns and not out.empty else None,
            out["datetime"].max() if "datetime" in out.columns and not out.empty else None,
        )

        return out

    except Exception:
        logger.exception("[YAHOO RESAMPLE] build_interval_frame failed interval=%s", interval)
        return pd.DataFrame()


# ============================================================
# compatibility aliases
# ============================================================

def build_yahoo_interval_frame(df_1min: pd.DataFrame, interval: int) -> pd.DataFrame:
    """
    互換alias。
    """
    return build_interval_frame(df_1min, interval=interval)


def resample_1m_to_interval(df_1min: pd.DataFrame, interval: int) -> pd.DataFrame:
    """
    互換alias。
    """
    return build_interval_frame(df_1min, interval=interval)


__all__ = [
    "resample_symbol_ohlcv",
    "build_interval_frame",
    "build_yahoo_interval_frame",
    "resample_1m_to_interval",
]