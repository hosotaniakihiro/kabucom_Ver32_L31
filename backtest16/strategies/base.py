# ============================================================
# backtest16/strategies/base.py
# ------------------------------------------------------------
# 戦略共通基盤。各戦略は detect(sd, params) -> list[Signal] を実装。
#
#  未来情報防止(§7)の徹底:
#   - 確定足は confirmed_bars_asof / confirm_ts で「確定済み」だけ参照。
#   - ブレイク検知はティックがあればティック、無ければ確定1分足の高安で代替。
#     いずれも参照は "その時刻まで" に限定し、シグナル時刻以降に約定させる。
#   - エントリー可能時間帯 9:05〜11:25 のシグナルのみ採用(§2)。
# ============================================================

from __future__ import annotations

import datetime as dt
from typing import List, Optional

import pandas as pd

from backtest16 import config as C
from backtest16.features import SymbolDay, confirm_ts
from backtest16.engine.execution_engine import Signal

REGISTRY: dict = {}


def register(cls):
    REGISTRY[cls.id] = cls()
    return cls


class Strategy:
    id = "BASE"
    side = "LONG"
    name = "base"

    def detect(self, sd: SymbolDay, params: dict) -> List[Signal]:
        raise NotImplementedError

    def _sig(self, sd, ts, ref_price, meta=None) -> Signal:
        return Signal(self.id, sd.symbol, sd.date, self.side, ts, ref_price, meta or {})


# ------------------------------------------------------------
# 時間帯ゲート
# ------------------------------------------------------------
def in_entry_window(ts: pd.Timestamp, params: dict) -> bool:
    start = params.get("entry_start", C.SESSION_ENTRY_START)
    end = params.get("entry_end", C.SESSION_ENTRY_END)
    t = ts.time()
    return start <= t <= end


def _dt_on(date, t: dt.time) -> pd.Timestamp:
    return pd.Timestamp(dt.datetime.combine(date, t))


# ------------------------------------------------------------
# ブレイク検知(ティック優先・無ければ確定1分足)
# ------------------------------------------------------------
def first_cross(sd: SymbolDay, level: float, direction: str,
                after_t: pd.Timestamp, until_t: pd.Timestamp) -> Optional[pd.Timestamp]:
    """level を上抜け(up)/下抜け(down)した最初の時刻(> after_t, <= until_t)。
    ティックがあれば1呼値相当の厳密クロスをティックで、無ければ確定1分足の高安で。"""
    tk = sd.ticks_after(after_t, until_t)
    if tk is not None and not tk.empty:
        if direction == "up":
            hit = tk[tk["price"] > level]
        else:
            hit = tk[tk["price"] < level]
        if not hit.empty:
            return hit.iloc[0]["ts"]
        return None
    # フォールバック: 確定1分足。足の高安が level を跨いだら、その足の確定時刻を返す。
    b = sd.bars1
    if b is None or b.empty:
        return None
    b = b[(b["ts"] > after_t - pd.Timedelta(minutes=1)) & (b["ts"] <= until_t)].sort_values("ts")
    for _, bar in b.iterrows():
        cft = confirm_ts(bar["ts"], 1)
        if cft <= after_t or cft > until_t:
            continue
        if direction == "up" and float(bar["high"]) > level:
            return cft
        if direction == "down" and float(bar["low"]) < level:
            return cft
    return None


def last_session_ts(sd: SymbolDay, params: dict) -> pd.Timestamp:
    return _dt_on(sd.date, params.get("entry_end", C.SESSION_ENTRY_END))


def vwap_at(sd: SymbolDay, ts: pd.Timestamp) -> Optional[float]:
    b = sd.confirmed_bars_asof(1, ts)
    if b is None or b.empty or "vwap" not in b.columns:
        return None
    v = b.iloc[-1].get("vwap")
    return float(v) if (v is not None and not pd.isna(v) and v > 0) else None


def gate(sd: SymbolDay, ts: Optional[pd.Timestamp], params: dict) -> bool:
    return ts is not None and in_entry_window(ts, params)
