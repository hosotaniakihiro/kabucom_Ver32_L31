# ============================================================
# File   : trading/summary/engine/summary_incremental_engine.py
# Version: PRODUCTION-STABLE-INCREMENTAL-SUMMARY-ENGINE-V2.0-DIRECT-1M-3M-5M
# ------------------------------------------------------------
# Purpose:
#   - PUSH tick DataFrame から 1分 / 3分 / 5分 サマリーを直接生成する
#   - 旧 summary_pipeline から呼ばれる run_incremental_summary_engine を提供する
#   - trading.summary.engine.common の互換関数不足で ImportError にならない
#   - 3分足 / 5分足が計算されない問題を避けるため、自己完結で resample する
#
# Design:
#   - 入力 push_df を symbol + datetime + close に正規化
#   - interval=1 は 1min slot、interval=3/5 は 3min/5min slot に floor
#   - OHLCV、MA、RSI、MACD、ATR、slope、score を最低限計算
#   - latest_only=True の場合は symbolごとの最新足のみ返す
#   - return は既存互換の dict(summary_df, summary_latest_df)
# ============================================================

from __future__ import annotations

import logging
from typing import Any, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# basic helpers
# ============================================================

def _as_df(obj: Any) -> pd.DataFrame:
    if isinstance(obj, pd.DataFrame):
        return obj.copy()
    if obj is None:
        return pd.DataFrame()
    try:
        return pd.DataFrame(obj).copy()
    except Exception:
        return pd.DataFrame()


def _first_existing(df: pd.DataFrame, candidates: list[str]) -> Optional[str]:
    for c in candidates:
        if c in df.columns:
            return c
    return None


def _safe_symbol_count(df: pd.DataFrame) -> int:
    try:
        if isinstance(df, pd.DataFrame) and not df.empty and "symbol" in df.columns:
            return int(df["symbol"].astype(str).nunique())
    except Exception:
        pass
    return 0


def _safe_latest_dt(df: pd.DataFrame):
    try:
        if isinstance(df, pd.DataFrame) and not df.empty and "datetime" in df.columns:
            s = pd.to_datetime(df["datetime"], errors="coerce").dropna()
            if not s.empty:
                return s.max()
    except Exception:
        pass
    return None


def _log_df_state(label: str, df: pd.DataFrame, interval: int | None = None) -> None:
    try:
        logger.info(
            "[INCREMENTAL SUMMARY][DF] label=%s interval=%s rows=%s cols=%s symbols=%s latest_dt=%s",
            label,
            interval,
            len(df) if isinstance(df, pd.DataFrame) else 0,
            len(df.columns) if isinstance(df, pd.DataFrame) else 0,
            _safe_symbol_count(df),
            _safe_latest_dt(df),
        )
    except Exception:
        logger.debug("[INCREMENTAL SUMMARY] log df state failed label=%s", label, exc_info=True)


def _normalize_push_df(push_df: Any) -> pd.DataFrame:
    df = _as_df(push_df)
    if df.empty:
        return df

    out = df.copy()

    sym_col = _first_existing(out, ["symbol", "Symbol", "code", "Code", "ticker", "Ticker"])
    if sym_col is None:
        logger.warning("[INCREMENTAL SUMMARY] symbol column missing cols=%s", list(out.columns))
        return pd.DataFrame()
    out["symbol"] = out[sym_col].astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
    out = out[out["symbol"] != ""].copy()

    name_col = _first_existing(out, ["symbolname", "SymbolName", "symbol_name", "name", "Name"])
    if name_col is not None:
        out["symbolname"] = out[name_col].astype(str)
    elif "symbolname" not in out.columns:
        out["symbolname"] = out["symbol"]

    dt_col = _first_existing(out, [
        "datetime", "dt", "timestamp", "received_at", "CurrentPriceTime",
        "current_price_time", "time", "Time", "last_update", "LastUpdate",
    ])
    if dt_col is None:
        logger.warning("[INCREMENTAL SUMMARY] datetime column missing cols=%s", list(out.columns))
        return pd.DataFrame()

    if dt_col in ("time", "Time") and "date" in out.columns:
        out["datetime"] = pd.to_datetime(out["date"].astype(str) + " " + out[dt_col].astype(str), errors="coerce")
    else:
        out["datetime"] = pd.to_datetime(out[dt_col], errors="coerce")

    try:
        out["datetime"] = out["datetime"].dt.tz_localize(None)
    except Exception:
        pass

    close_col = _first_existing(out, [
        "close", "close_price", "price", "Price", "current_price", "CurrentPrice",
        "last_price", "LastPrice", "Close", "ClosePrice",
    ])
    if close_col is None:
        logger.warning("[INCREMENTAL SUMMARY] close/price column missing cols=%s", list(out.columns))
        return pd.DataFrame()

    out["close"] = pd.to_numeric(out[close_col], errors="coerce")

    for target, candidates in {
        "open": ["open", "open_price", "Open", "OpenPrice"],
        "high": ["high", "high_price", "High", "HighPrice"],
        "low": ["low", "low_price", "Low", "LowPrice"],
    }.items():
        c = _first_existing(out, candidates)
        if c is not None:
            out[target] = pd.to_numeric(out[c], errors="coerce")
        else:
            out[target] = out["close"]

    vol_col = _first_existing(out, [
        "volume", "Volume", "trading_volume", "TradingVolume", "CumVolume",
        "cum_volume", "last_cum_volume",
    ])
    if vol_col is not None:
        out["volume"] = pd.to_numeric(out[vol_col], errors="coerce")
    else:
        out["volume"] = 0.0

    out = out.dropna(subset=["symbol", "datetime", "close"]).copy()
    if out.empty:
        return out

    out = out.sort_values(["symbol", "datetime"], kind="stable").reset_index(drop=True)

    _log_df_state("normalized_push", out)
    return out


# ============================================================
# indicators
# ============================================================

def _calc_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    try:
        delta = close.diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=1).mean()
        avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=1).mean()
        rs = avg_gain / avg_loss.replace(0, np.nan)
        return (100 - (100 / (1 + rs))).fillna(50.0)
    except Exception:
        return pd.Series([50.0] * len(close), index=close.index)


def _add_indicators(df: pd.DataFrame, interval: int) -> pd.DataFrame:
    if df.empty:
        return df

    parts: list[pd.DataFrame] = []
    for sym, one in df.groupby("symbol", sort=False):
        one = one.copy().sort_values("datetime", kind="stable").reset_index(drop=True)

        close = pd.to_numeric(one["close"], errors="coerce")
        high = pd.to_numeric(one["high"], errors="coerce").combine_first(close)
        low = pd.to_numeric(one["low"], errors="coerce").combine_first(close)
        open_ = pd.to_numeric(one["open"], errors="coerce").combine_first(close)
        volume = pd.to_numeric(one.get("volume", 0), errors="coerce").fillna(0.0)

        one["ma5"] = close.rolling(5, min_periods=1).mean()
        one["ma25"] = close.rolling(25, min_periods=1).mean()
        one["ma75"] = close.rolling(75, min_periods=1).mean()
        one["rsi"] = _calc_rsi(close)

        ema12 = close.ewm(span=12, adjust=False, min_periods=1).mean()
        ema26 = close.ewm(span=26, adjust=False, min_periods=1).mean()
        one["macd"] = ema12 - ema26
        one["signal"] = one["macd"].ewm(span=9, adjust=False, min_periods=1).mean()
        one["hist"] = one["macd"] - one["signal"]

        prev_close = close.shift(1)
        tr = pd.concat([
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ], axis=1).max(axis=1)
        one["atr"] = tr.ewm(alpha=1 / 14, adjust=False, min_periods=1).mean().fillna(0.0)

        one["slope"] = close.pct_change().fillna((close - open_) / open_.replace(0, np.nan)).fillna(0.0)
        one["slope_atr_scaled"] = one["slope"] / one["atr"].replace(0, np.nan)
        one["slope_atr_scaled"] = pd.to_numeric(one["slope_atr_scaled"], errors="coerce").replace([np.inf, -np.inf], np.nan).fillna(0.0)

        one["vwap"] = close
        try:
            cum_pv = (close * volume).groupby(one["datetime"].dt.date).cumsum()
            cum_v = volume.groupby(one["datetime"].dt.date).cumsum().replace(0, np.nan)
            one["vwap"] = (cum_pv / cum_v).combine_first(close)
        except Exception:
            pass

        base = one["slope"].fillna(0.0) * 100.0
        macd_bonus = np.where(one["macd"] > one["signal"], 1.0, -1.0)
        rsi_bonus = (one["rsi"].fillna(50.0) - 50.0) / 10.0
        one["score"] = base + macd_bonus + rsi_bonus
        one["score_buy"] = one["score"].clip(lower=0)
        one["score_sell"] = (-one["score"]).clip(lower=0)
        one["score_slope"] = base
        if "score_mtf" not in one.columns:
            one["score_mtf"] = 0.0
        one["score_total"] = one["score"] + pd.to_numeric(one["score_mtf"], errors="coerce").fillna(0.0)
        one["final_score"] = one["score_total"]
        one["display_score"] = one["score_total"]
        one["technical_ready"] = True
        one["symbol_hist_len"] = range(1, len(one) + 1)
        one["interval"] = int(interval)
        one["source"] = f"push_incremental_{int(interval)}min"

        parts.append(one)

    out = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    _log_df_state("after_indicators", out, interval)
    return out


# ============================================================
# builders
# ============================================================

def _build_bars(push_df: pd.DataFrame, interval: int) -> pd.DataFrame:
    ticks = _normalize_push_df(push_df)
    if ticks.empty:
        return pd.DataFrame()

    interval = int(interval)
    freq = f"{interval}min"

    work = ticks.copy()
    work["_slot"] = pd.to_datetime(work["datetime"], errors="coerce").dt.floor(freq)
    work = work.dropna(subset=["_slot"]).copy()
    work = work.sort_values(["symbol", "datetime"], kind="stable")

    def _last_text(s: pd.Series):
        x = s.dropna().astype(str)
        return x.iloc[-1] if not x.empty else ""

    bars = (
        work.groupby(["symbol", "_slot"], as_index=False)
        .agg(
            symbolname=("symbolname", _last_text),
            open=("close", "first"),
            high=("high", "max"),
            low=("low", "min"),
            close=("close", "last"),
            volume=("volume", "max"),
            tick_count=("close", "count"),
            first_tick_at=("datetime", "min"),
            last_tick_at=("datetime", "max"),
        )
        .rename(columns={"_slot": "datetime"})
    )

    for c in ("open", "high", "low", "close", "volume"):
        bars[c] = pd.to_numeric(bars[c], errors="coerce")

    bars["open_price"] = bars["open"]
    bars["high_price"] = bars["high"]
    bars["low_price"] = bars["low"]
    bars["close_price"] = bars["close"]
    bars["price"] = bars["close"]
    bars["current_price"] = bars["close"]
    bars["date"] = pd.to_datetime(bars["datetime"], errors="coerce").dt.strftime("%Y-%m-%d")
    bars["time"] = pd.to_datetime(bars["datetime"], errors="coerce").dt.strftime("%H:%M:%S")
    bars["start_time"] = bars["time"]
    bars["end_time"] = bars["time"]
    bars["time_range"] = pd.to_datetime(bars["datetime"], errors="coerce").dt.strftime("%H:%M")

    bars = bars.dropna(subset=["symbol", "datetime", "close"]).copy()
    bars = bars.sort_values(["symbol", "datetime"], kind="stable").reset_index(drop=True)

    out = _add_indicators(bars, interval)
    _log_df_state("built_bars", out, interval)
    return out


def _latest_per_symbol(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or "symbol" not in df.columns or "datetime" not in df.columns:
        return df
    out = df.copy()
    out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
    out = out.dropna(subset=["symbol", "datetime"]).copy()
    out = out.sort_values(["symbol", "datetime"], kind="stable")
    return out.groupby("symbol", as_index=False).tail(1).reset_index(drop=True)


# ============================================================
# public API
# ============================================================

def run_incremental_summary_engine(
    *,
    interval: int = 1,
    summary_df: Any = None,
    push_df: Any = None,
    evaluate_signals: bool = True,
    latest_only: bool = False,
    recent_bars_per_symbol: int = 120,
    **kwargs: Any,
) -> dict[str, pd.DataFrame]:
    interval = int(interval)

    logger.info(
        "[INCREMENTAL SUMMARY] run start interval=%s push_rows=%s summary_rows=%s latest_only=%s recent_bars=%s evaluate_signals=%s",
        interval,
        len(push_df) if isinstance(push_df, pd.DataFrame) else 0,
        len(summary_df) if isinstance(summary_df, pd.DataFrame) else 0,
        latest_only,
        recent_bars_per_symbol,
        evaluate_signals,
    )

    bars = _build_bars(_as_df(push_df), interval)
    if bars.empty:
        logger.warning("[INCREMENTAL SUMMARY] run empty interval=%s", interval)
        empty = pd.DataFrame()
        return {"interval": interval, "summary_df": empty, "summary_latest_df": empty}

    if recent_bars_per_symbol and recent_bars_per_symbol > 0:
        bars = (
            bars.sort_values(["symbol", "datetime"], kind="stable")
            .groupby("symbol", as_index=False)
            .tail(int(recent_bars_per_symbol))
            .reset_index(drop=True)
        )

    latest = _latest_per_symbol(bars) if latest_only else bars.copy()

    logger.info(
        "[INCREMENTAL SUMMARY] run done interval=%s rows=%s latest_rows=%s symbols=%s latest_dt=%s",
        interval,
        len(bars),
        len(latest),
        _safe_symbol_count(bars),
        _safe_latest_dt(bars),
    )

    return {
        "interval": interval,
        "summary_df": bars,
        "summary_latest_df": latest,
    }


def build_incremental_summary(*args: Any, **kwargs: Any):
    return run_incremental_summary_engine(*args, **kwargs)


def process_single_interval(df_push: pd.DataFrame, interval: int) -> dict[str, pd.DataFrame]:
    return run_incremental_summary_engine(interval=interval, push_df=df_push, latest_only=False)


def build_1m_from_push(df_push: pd.DataFrame) -> pd.DataFrame:
    return _build_bars(df_push, 1)


def build_target_interval_df(df_push: pd.DataFrame, interval: int) -> pd.DataFrame:
    return _build_bars(df_push, int(interval))


__all__ = [
    "run_incremental_summary_engine",
    "build_incremental_summary",
    "process_single_interval",
    "build_1m_from_push",
    "build_target_interval_df",
]
