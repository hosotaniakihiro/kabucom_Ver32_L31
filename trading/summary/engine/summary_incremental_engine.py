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

def _ranking_filter_rescue_period_volume_from_cumulative(
    df: pd.DataFrame, *, volume_col: str = "volume", datetime_col: str = "datetime"
) -> pd.DataFrame:
    """累計出来高を symbol + 当日単位で差分化し、対象足出来高へ変換する。

    旧 core/startup/ranking_entry_filter_rescue_patch V1.7 の
    _period_volume_from_cumulative を移設 (_build_bars / ranking_technical_store
    ._calculate_technicals の双方で個別に使うため、本ファイルにも複製している)。
    """
    if df is None or getattr(df, "empty", True) or volume_col not in df.columns:
        return df
    out = df.copy()
    raw = pd.to_numeric(out[volume_col], errors="coerce").fillna(0.0)
    out["_raw_cumulative_volume"] = raw
    if "symbol" not in out.columns or datetime_col not in out.columns:
        out["_period_volume"] = raw.clip(lower=0.0)
        return out
    dt_s = pd.to_datetime(out[datetime_col], errors="coerce")
    out["_trade_date_for_volume"] = dt_s.dt.date
    out["_volume_order"] = range(len(out))
    out = out.sort_values(["symbol", "_trade_date_for_volume", datetime_col, "_volume_order"], kind="stable")
    prev = out.groupby(["symbol", "_trade_date_for_volume"], sort=False)["_raw_cumulative_volume"].shift(1)
    diff = out["_raw_cumulative_volume"] - prev
    # 初回行は直前累計が不明なので0扱い。累計リセット/異常なマイナスも0にする。
    period = diff.where(prev.notna(), 0.0)
    period = period.where(period >= 0, 0.0).replace([np.inf, -np.inf], 0.0).fillna(0.0)
    out["_period_volume"] = period
    out = out.sort_values("_volume_order", kind="stable").drop(columns=["_volume_order", "_trade_date_for_volume"], errors="ignore")
    return out


def _seed_period_volume_from_prev(
    work: pd.DataFrame, prev_cumulative: dict[str, float] | None
) -> pd.DataFrame:
    """tick を切り出したときに落ちる「先頭行の期間出来高」を前の足から補う。

    _ranking_filter_rescue_period_volume_from_cumulative は
    symbol+当日単位で shift(1) するため、各 symbol の先頭行は period=0 になる。
    全期間を毎回読み直している間はそれで正しかったが、直近ぶんだけ切り出すと
    その 0 が「本当は取引があった分」を捨ててしまう。
    キャッシュが持っている1つ前の足の cumulative_volume を使って埋める。
    """
    if not prev_cumulative or work is None or work.empty:
        return work
    if "_raw_cumulative_volume" not in work.columns or "symbol" not in work.columns:
        return work

    try:
        out = work
        # 先頭行 = symbol+当日 の中で最初に現れる行
        dt_s = pd.to_datetime(out["datetime"], errors="coerce")
        grp = [out["symbol"].astype(str), dt_s.dt.date]
        is_first = out.groupby(grp, sort=False).cumcount().eq(0)

        seed = out["symbol"].astype(str).map(lambda s: prev_cumulative.get(s))
        seed = pd.to_numeric(seed, errors="coerce")
        raw = pd.to_numeric(out["_raw_cumulative_volume"], errors="coerce")

        target = is_first & seed.notna() & raw.notna()
        if not bool(target.any()):
            return out

        filled = (raw - seed).clip(lower=0.0)
        out = out.copy()
        out.loc[target, "_period_volume"] = filled.loc[target].fillna(0.0)
        return out
    except Exception:
        logger.debug("[INCREMENTAL SUMMARY] period volume seed failed", exc_info=True)
        return work


def _build_bars_core(push_df: pd.DataFrame, interval: int) -> pd.DataFrame:
    """期間出来高補正 (下記 _build_bars 参照) より前の素の足生成ロジック。

    現在は _build_bars から呼ばれない (下の補正版が常時有効だったため)。
    比較・参照用に残している。
    """
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


def _build_bars(push_df: pd.DataFrame, interval: int) -> pd.DataFrame:
    """PUSH tick DataFrame から足を作る (期間出来高補正版)。

    旧 core/startup/ranking_entry_filter_rescue_patch V1.7 が (env gate なしで
    無条件に) この関数を差し替え、1分/3分/5分足の volume を「当日累計出来高の
    max」ではなく「symbol+当日単位で累計出来高を diff した期間出来高の sum」に
    補正していた。ranking/tonosama 側の出来高フィルタが累計値のまま誤判定しない
    ための補正で、常時有効だったためそのまま本文化する。
    """
    interval_n = int(interval)

    from trading.summary.engine.incremental import bar_cache

    mode = bar_cache.get_mode()
    if mode == "off":
        out = _add_indicators(_build_raw_bars(push_df, interval_n), interval_n)
        _log_df_state("built_bars_period_volume_guard", out, interval_n)
        return out

    ticks = _normalize_push_df(push_df)
    if ticks.empty:
        return pd.DataFrame()

    def _raw_builder(*, interval: int, ticks: pd.DataFrame, prev_cumulative):
        return _build_raw_bars(None, interval, ticks=ticks, prev_cumulative=prev_cumulative)

    if mode == "shadow" and not bar_cache.shadow_should_compare(interval_n):
        # 間引かれた tick はキャッシュだけ更新して従来経路を返す
        # (更新しないとキャッシュに穴があき、次の比較が偽の差分になる)
        bar_cache.build_raw_bars_cached(
            interval=interval_n, ticks=ticks, raw_builder=_raw_builder
        )
        out = _add_indicators(_build_raw_bars(None, interval_n, ticks=ticks), interval_n)
        _log_df_state("built_bars_period_volume_guard", out, interval_n)
        return out

    cached_raw = bar_cache.build_raw_bars_cached(
        interval=interval_n, ticks=ticks, raw_builder=_raw_builder
    )
    cached_out = _add_indicators(cached_raw, interval_n)

    if mode == "on":
        _log_df_state("built_bars_period_volume_guard", cached_out, interval_n)
        return cached_out

    # shadow: 両方作って差分をログに出し、返すのは従来の結果
    full_out = _add_indicators(_build_raw_bars(None, interval_n, ticks=ticks), interval_n)
    try:
        info = bar_cache.compare_for_shadow(full_out, cached_out, interval_n)
        log = logger.warning if info.get("status") != "identical" else logger.info
        log("[BAR CACHE][SHADOW] %s", info)
    except Exception:
        logger.exception("[BAR CACHE][SHADOW] compare failed interval=%s", interval_n)
    _log_df_state("built_bars_period_volume_guard", full_out, interval_n)
    return full_out


def _build_raw_bars(
    push_df: pd.DataFrame,
    interval: int,
    *,
    prev_cumulative: dict[str, float] | None = None,
    ticks: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """指標を付ける前の素の足を作る (_build_bars の前半)。

    prev_cumulative:
        差分キャッシュ用。symbol -> 「このtick集合より前の時点での累計出来高」。
        期間出来高は symbol+当日単位で累計出来高を shift(1) して差分するため、
        tick を直近ぶんだけ切り出すと各 symbol の先頭行の period が 0 になり、
        その足の出来高が過少になる。前の足の cumulative_volume を渡して補う。
        None のときは従来どおり (先頭行は 0 扱い)。
    """
    src = _normalize_push_df(push_df) if ticks is None else ticks
    if src is None or src.empty:
        return pd.DataFrame()

    interval_n = int(interval)
    freq = f"{interval_n}min"
    work = src.copy()
    work["datetime"] = pd.to_datetime(work["datetime"], errors="coerce")
    work = work.dropna(subset=["datetime"]).copy()
    work = work.sort_values(["symbol", "datetime"], kind="stable")
    work = _ranking_filter_rescue_period_volume_from_cumulative(work, volume_col="volume", datetime_col="datetime")
    work = _seed_period_volume_from_prev(work, prev_cumulative)
    work["_slot"] = work["datetime"].dt.floor(freq)
    work = work.dropna(subset=["_slot"]).copy()

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
            volume=("_period_volume", "sum"),
            cumulative_volume=("_raw_cumulative_volume", "max"),
            tick_count=("close", "count"),
            first_tick_at=("datetime", "min"),
            last_tick_at=("datetime", "max"),
        )
        .rename(columns={"_slot": "datetime"})
    )

    for c in ("open", "high", "low", "close", "volume", "cumulative_volume"):
        if c in bars.columns:
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
    return bars


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
