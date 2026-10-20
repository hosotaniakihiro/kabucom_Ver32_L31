# ============================================================
# backtest16/features.py
# ------------------------------------------------------------
# 戦略が参照する「銘柄×日」単位の特徴量（§4/§5/§7）。
#
#  中核: SymbolDay — ある銘柄のある立会日について、
#    - bars1/bars3/bars5 : 確定足(summary由来, ts=足ラベル=開始時刻)
#    - ticks             : PUSHスナップショット(ts,price,板)。無い銘柄は空。
#    - prev_close        : 前営業日の終値(前日summary5minの最終close)
#    - day_open          : 当日始値(最初の1分足のopen, 09:00以降)
#    - gap_pct           : (day_open - prev_close)/prev_close*100
#
#  未来情報防止(§7)の鉄則:
#    - 確定足の「確定時刻」= ts(ラベル=開始) + interval分。
#      戦略は confirm_ts <= 現在時刻 の足だけ参照する。
#    - 最初の5分足は 09:05 確定まで使わない。9:15レンジは 09:15 まで使わない。
#    - VWAP/MAは確定足に保存済みの値を使う(その足の確定時点までの情報)。
# ============================================================

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

from backtest16 import config as C


def confirm_ts(bar_ts: pd.Timestamp, interval_min: int) -> pd.Timestamp:
    """足ラベル(開始時刻)から確定時刻を返す。"""
    return bar_ts + pd.Timedelta(minutes=interval_min)


@dataclass
class SymbolDay:
    symbol: str
    date: dt.date
    bars1: pd.DataFrame            # 確定1分足
    bars3: pd.DataFrame            # 確定3分足
    bars5: pd.DataFrame            # 確定5分足
    ticks: pd.DataFrame            # PUSHスナップショット(空可)
    prev_close: Optional[float]
    prev_high: Optional[float] = None   # 前営業日の高値(SHORT_10用。分割調整は要注記)
    day_open: Optional[float] = None
    gap_pct: Optional[float] = None
    first5_high: Optional[float] = None   # 最初の5分足(09:00-09:05)の高値
    first5_low: Optional[float] = None
    first5_bullish: Optional[bool] = None # 最初の5分足が陽線か
    first5_ok: bool = False               # 最初の5分足が完全に取れたか
    meta: dict = field(default_factory=dict)

    def __post_init__(self):
        self._compute_open_gap()
        self._compute_first5()

    # --- 当日始値・ギャップ ---
    def _compute_open_gap(self):
        if self.bars1 is not None and not self.bars1.empty:
            b = self.bars1[self.bars1["ts"].dt.time >= C.FIRST_BAR_START]
            if not b.empty:
                self.day_open = float(b.sort_values("ts").iloc[0]["open"])
        if self.day_open and self.prev_close and self.prev_close > 0:
            self.gap_pct = (self.day_open - self.prev_close) / self.prev_close * 100.0

    # --- 最初の5分足(09:00-09:05) ---
    def _compute_first5(self):
        # 5分足テーブルに 09:00 ラベルの足があればそれを使う(最優先)
        if self.bars5 is not None and not self.bars5.empty:
            f = self.bars5[self.bars5["ts"].dt.time == C.FIRST_BAR_START]
            if not f.empty:
                row = f.sort_values("ts").iloc[0]
                self.first5_high = float(row["high"])
                self.first5_low = float(row["low"])
                self.first5_bullish = bool(row["close"] >= row["open"])
                self.first5_ok = True
                return
        # 無ければ 1分足 09:00-09:04 の5本から合成
        if self.bars1 is not None and not self.bars1.empty:
            m = self.bars1[(self.bars1["ts"].dt.time >= C.FIRST_BAR_START) &
                           (self.bars1["ts"].dt.time < C.FIRST_BAR_LOCK)]
            if len(m) >= 1:
                self.first5_high = float(m["high"].max())
                self.first5_low = float(m["low"].min())
                o = float(m.sort_values("ts").iloc[0]["open"])
                c = float(m.sort_values("ts").iloc[-1]["close"])
                self.first5_bullish = bool(c >= o)
                # 5本すべて揃っていれば完全(≥5本)。欠損時は不完全フラグ。
                self.first5_ok = len(m) >= 5

    # --- 確定足アクセサ(未来情報防止) ---
    def bars(self, interval: int) -> pd.DataFrame:
        return {1: self.bars1, 3: self.bars3, 5: self.bars5}[interval]

    def confirmed_bars_asof(self, interval: int, now: pd.Timestamp) -> pd.DataFrame:
        """now 時点で既に確定している足のみ(confirm_ts <= now)。昇順。"""
        b = self.bars(interval)
        if b is None or b.empty:
            return b
        conf = b["ts"] + pd.Timedelta(minutes=interval)
        return b[conf <= now].sort_values("ts")

    def ticks_after(self, t: pd.Timestamp, until: Optional[pd.Timestamp] = None) -> pd.DataFrame:
        """t より後(厳密 >)のPUSHスナップショット。実約定順序の捏造を避けるため実データ順。"""
        if self.ticks is None or self.ticks.empty:
            return self.ticks if self.ticks is not None else pd.DataFrame()
        m = self.ticks["ts"] > t
        if until is not None:
            m &= self.ticks["ts"] <= until
        return self.ticks[m].sort_values("ts")


# ------------------------------------------------------------
# ギャップ分類(§13 追加分析のためのバケット)
# ------------------------------------------------------------
def gap_bucket(gap_pct: Optional[float]) -> str:
    if gap_pct is None:
        return "UNKNOWN"
    g = gap_pct
    if g >= 7:
        return "GU>=7"
    if g >= 5:
        return "GU5-7"
    if g >= 3:
        return "GU3-5"
    if g > -3:
        return "FLAT(-3,3)"
    if g > -5:
        return "GD3-5"
    if g > -7:
        return "GD5-7"
    return "GD<=-7"


def volume_avg_prev(bars: pd.DataFrame, idx: int, lookback: int) -> Optional[float]:
    """idx 番目の足の直前 lookback 本の平均出来高(評価足 idx は含めない=§7)。"""
    if idx < lookback:
        return None
    window = bars.iloc[idx - lookback: idx]
    if len(window) < lookback:
        return None
    v = pd.to_numeric(window["volume"], errors="coerce").dropna()
    if v.empty:
        return None
    return float(v.mean())


def rolling_prev_extreme(bars: pd.DataFrame, idx: int, lookback: int, kind: str) -> Optional[float]:
    """idx の直前 lookback 本の高値max / 安値min(評価足は含めない)。"""
    if idx < lookback:
        return None
    window = bars.iloc[idx - lookback: idx]
    if len(window) < lookback:
        return None
    if kind == "high":
        return float(pd.to_numeric(window["high"], errors="coerce").max())
    return float(pd.to_numeric(window["low"], errors="coerce").min())


def range_high_low(bars: pd.DataFrame, start_t: dt.time, end_t: dt.time) -> tuple:
    """[start_t, end_t) ラベルの足から高値max/安値min。9:15レンジ等。"""
    b = bars[(bars["ts"].dt.time >= start_t) & (bars["ts"].dt.time < end_t)]
    if b.empty:
        return (None, None)
    return (float(b["high"].max()), float(b["low"].min()))
