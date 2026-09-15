# ============================================================
# trading/summary/mtf_local_bars.py
# ------------------------------------------------------------
# ローカル push DB (ティック) から、銘柄ごとの 3 分足 / 5 分足を作り、
# 指標 (ma5 / ma25 / ma75 / rsi / macd / signal / hist / vwap) を付ける。
#
# 2026-09-15 の実測: main.py が持つ 3m/5m の in-memory 履歴は
#   tf=3 rows=156 (50 銘柄で 3 本ずつ) / rsi=0 macd=0 (指標が無い)
# で、[MTF MA GATE] が午前中 119 件すべて却下 (rsi_nan / macd_nan /
# 6 分間同じ値の 5m 足) になっていた。summary DB (NAS) は遅く、
# 読めない時間帯があるので、ローカルに毎秒書かれている push DB
# (%LOCALAPPDATA%\autostock\push\pushYYYYMMDD.db、(symbol, datetime) 索引あり、
# 1 銘柄 2 万ティックで 0.08 秒) から自前で足を作る。
#
# 足の作り方は summary_incremental_engine と同じ:
#   open=最初のティック価格 / high=max / low=min / close=最後 / volume=累計出来高の差分
#   ma: rolling(min_periods=1) / rsi: ewm(alpha=1/14) / macd: ema12-ema26, signal ema9
# ============================================================
from __future__ import annotations

import datetime as dt
import logging
import os
import sqlite3
import threading
from pathlib import Path
from typing import Dict, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

VERSION = "V1.0-MTF-LOCAL-BARS-FROM-PUSH-DB"

_CACHE_TTL_SEC = 10.0
_lock = threading.Lock()
_cache: Dict[Tuple[str, int], Tuple[float, pd.DataFrame]] = {}


def _push_db_path(day: Optional[dt.date] = None) -> Optional[Path]:
    d = (day or dt.date.today()).strftime("%Y%m%d")
    cands = []
    env_dir = os.getenv("PUSH_LOCAL_DB_DIR", "").strip()
    if env_dir:
        cands.append(Path(env_dir) / f"push{d}.db")
    la = os.getenv("LOCALAPPDATA", "")
    if la:
        cands.append(Path(la) / "autostock" / "push" / f"push{d}.db")
    cands.append(Path(r"X:\raw_data\kabu_station\push") / f"push{d}.db")
    for p in cands:
        try:
            if p.exists():
                return p
        except Exception:
            continue
    return None


def _norm_symbol(v) -> str:
    s = str(v or "").strip()
    if s.endswith(".0"):
        s = s[:-2]
    return s


def _load_ticks(symbol: str, day: dt.date) -> pd.DataFrame:
    p = _push_db_path(day)
    if p is None:
        return pd.DataFrame()
    since = day.strftime("%Y-%m-%dT09:00")
    try:
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=1.0)
        try:
            con.execute("PRAGMA busy_timeout=1000")
            df = pd.read_sql_query(
                "SELECT datetime, price, volume FROM stream_data_raw "
                "WHERE symbol=? AND datetime >= ? ORDER BY datetime",
                con, params=(symbol, since),
            )
        finally:
            con.close()
    except Exception:
        logger.debug("[MTF LOCAL BARS] tick read failed symbol=%s path=%s", symbol, p, exc_info=True)
        return pd.DataFrame()
    if df.empty:
        return df
    df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
    try:
        df["datetime"] = df["datetime"].dt.tz_localize(None)
    except Exception:
        pass
    df["price"] = pd.to_numeric(df["price"], errors="coerce")
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce")
    return df.dropna(subset=["datetime", "price"])


def _add_indicators(bars: pd.DataFrame) -> pd.DataFrame:
    close = bars["close"]
    bars["ma5"] = close.rolling(5, min_periods=1).mean()
    bars["ma25"] = close.rolling(25, min_periods=1).mean()
    bars["ma75"] = close.rolling(75, min_periods=1).mean()
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / 14, adjust=False, min_periods=1).mean()
    avg_loss = loss.ewm(alpha=1 / 14, adjust=False, min_periods=1).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    bars["rsi"] = (100 - (100 / (1 + rs))).fillna(50.0)
    ema12 = close.ewm(span=12, adjust=False, min_periods=1).mean()
    ema26 = close.ewm(span=26, adjust=False, min_periods=1).mean()
    bars["macd"] = ema12 - ema26
    bars["signal"] = bars["macd"].ewm(span=9, adjust=False, min_periods=1).mean()
    bars["hist"] = bars["macd"] - bars["signal"]
    vol = bars["volume"].clip(lower=0)
    cum_pv = (close * vol).cumsum()
    cum_v = vol.cumsum().replace(0, np.nan)
    bars["vwap"] = (cum_pv / cum_v).fillna(close)
    bars["slope"] = close.pct_change().fillna(0.0)
    return bars


def get_bars(symbol, tf: int, now: Optional[dt.datetime] = None) -> pd.DataFrame:
    """当日の tf 分足 (指標付き)。列: symbol, datetime(足の開始), open, high, low, close,
    volume, tick_count, ma5, ma25, ma75, rsi, macd, signal, hist, vwap, slope, completed。
    空なら空の DataFrame。"""
    sym = _norm_symbol(symbol)
    tf = int(tf)
    now = now or dt.datetime.now()
    key = (sym, tf)
    ts = now.timestamp()
    with _lock:
        hit = _cache.get(key)
        if hit and ts - hit[0] <= _CACHE_TTL_SEC:
            return hit[1]
    ticks = _load_ticks(sym, now.date())
    if ticks.empty:
        bars = pd.DataFrame()
    else:
        t = ticks.set_index("datetime")
        slot = t.index.floor(f"{tf}min")
        g = t.groupby(slot)
        bars = pd.DataFrame({
            "open": g["price"].first(),
            "high": g["price"].max(),
            "low": g["price"].min(),
            "close": g["price"].last(),
            "cum_volume": g["volume"].max(),
            "tick_count": g["price"].count(),
        })
        bars["volume"] = bars["cum_volume"].diff().fillna(bars["cum_volume"]).clip(lower=0)
        bars.index.name = "datetime"
        bars = bars.reset_index()
        bars["symbol"] = sym
        bars = _add_indicators(bars)
        # 旧 conditions_long/short のヘルパー (_is_bull 等) は *_price 列名を見る
        for k in ("open", "high", "low", "close"):
            bars[f"{k}_price"] = bars[k]
        bars["current_price"] = bars["close"]
        bars["completed"] = (bars["datetime"] + pd.Timedelta(minutes=tf)) <= now
    with _lock:
        _cache[key] = (ts, bars)
    return bars


def latest_bar(symbol, tf: int, now: Optional[dt.datetime] = None, *, completed_only: bool = False,
               max_age_bars: float = 2.0) -> Optional[pd.Series]:
    """最新足。古すぎる足 (最終ティックが今から tf×max_age_bars 分より前) は None。
    PUSH 登録から外れた銘柄は足が止まるので、そのまま判定に使うと
    朝の値で午後に判定してしまう (8001: 09:09 の足しか無い状態を実測)。"""
    now = now or dt.datetime.now()
    bars = get_bars(symbol, tf, now)
    if bars is None or bars.empty:
        return None
    if completed_only:
        bars = bars[bars["completed"]]
        if bars.empty:
            return None
    r = bars.iloc[-1]
    try:
        start = r["datetime"].to_pydatetime() if hasattr(r["datetime"], "to_pydatetime") else r["datetime"]
        if max_age_bars and start + dt.timedelta(minutes=tf * max_age_bars) < now:
            return None
    except Exception:
        pass
    return r


def is_stale(bars: pd.DataFrame, tf: int, now: Optional[dt.datetime] = None, max_age_bars: float = 2.0) -> bool:
    if bars is None or bars.empty:
        return True
    now = now or dt.datetime.now()
    try:
        start = bars.iloc[-1]["datetime"]
        start = start.to_pydatetime() if hasattr(start, "to_pydatetime") else start
        return start + dt.timedelta(minutes=tf * max_age_bars) < now
    except Exception:
        return True
