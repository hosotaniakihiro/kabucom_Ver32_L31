# backtest16 テスト用 合成データヘルパ(ネットワーク/実DB不要)
from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from backtest16.features import SymbolDay

DAY = dt.date(2026, 6, 16)


def ts(h, m, s=0):
    return pd.Timestamp(dt.datetime.combine(DAY, dt.time(h, m, s)))


def bar(h, m, o, hi, lo, c, v=10000, vwap=None, ma5=None, ma25=None, interval=1):
    return {"ts": ts(h, m), "open": o, "high": hi, "low": lo, "close": c, "volume": v,
            "vwap": vwap if vwap is not None else c, "ma5": ma5, "ma25": ma25, "ma75": None,
            "interval_min": interval}


def bars_df(rows):
    return pd.DataFrame(rows)


def make_symbol_day(bars1=None, bars3=None, bars5=None, ticks=None,
                    prev_close=1000.0, prev_high=None, symbol="7777"):
    empty = pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume", "vwap", "ma5", "ma25"])
    tk = ticks if ticks is not None else pd.DataFrame(columns=["symbol", "ts", "price", "ask1", "bid1"])
    return SymbolDay(
        symbol=symbol, date=DAY,
        bars1=bars_df(bars1) if bars1 else empty.copy(),
        bars3=bars_df(bars3) if bars3 else empty.copy(),
        bars5=bars_df(bars5) if bars5 else empty.copy(),
        ticks=tk, prev_close=prev_close, prev_high=prev_high,
    )


def ticks_df(rows):
    # rows: (h,m,s,price[,ask1,bid1])
    out = []
    for r in rows:
        h, m, s, px = r[0], r[1], r[2], r[3]
        d = {"symbol": "7777", "ts": ts(h, m, s), "price": px}
        if len(r) > 4:
            d["ask1"] = r[4]
        if len(r) > 5:
            d["bid1"] = r[5]
        out.append(d)
    return pd.DataFrame(out)


@pytest.fixture
def first5_bars_1min():
    """09:00-09:04 の5本(初動5分足 high=1040, low=1030)。"""
    return [
        bar(9, 0, 1030, 1035, 1030, 1032),
        bar(9, 1, 1032, 1038, 1031, 1036),
        bar(9, 2, 1036, 1040, 1035, 1039),
        bar(9, 3, 1039, 1040, 1037, 1038),
        bar(9, 4, 1038, 1039, 1036, 1037),
    ]
