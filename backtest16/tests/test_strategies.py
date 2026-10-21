# 買い/空売り戦略の検知と方向性(§16)
from __future__ import annotations

import pandas as pd
from backtest16.tests.conftest import make_symbol_day, bar, ts
from backtest16.strategies.longs import Long01GapUpBreak
from backtest16.strategies.shorts import Short01GapUpLow, Short05GapDownLow


def _gu_bars(first5_bars_1min, break_bar):
    rows = list(first5_bars_1min)
    rows.append(bar(9, 5, 1037, 1038, 1036, 1037))
    rows.append(break_bar)
    return rows


def test_long01_fires_on_gap_and_breakout(first5_bars_1min):
    # prev_close=1000, day_open(09:00 open)=1030 → gap=+3.0%。first5_high=1040。09:06でhigh1045ブレイク
    rows = _gu_bars(first5_bars_1min, bar(9, 6, 1038, 1045, 1037, 1044))
    sd = make_symbol_day(bars1=rows, prev_close=1000.0)
    assert abs(sd.gap_pct - 3.0) < 1e-6
    assert sd.first5_ok and sd.first5_high == 1040
    sigs = Long01GapUpBreak().detect(sd, {})
    assert len(sigs) == 1
    assert sigs[0].side == "LONG"
    assert sigs[0].signal_ts.time().hour == 9 and sigs[0].signal_ts >= ts(9, 5)


def test_long01_no_fire_without_gap(first5_bars_1min):
    # day_open=1010 → gap=+1% (<3%) → 発火しない
    rows = list(first5_bars_1min)
    rows[0] = bar(9, 0, 1010, 1035, 1010, 1032)   # open=1010
    rows.append(bar(9, 6, 1038, 1045, 1037, 1044))
    sd = make_symbol_day(bars1=rows, prev_close=1000.0)
    assert sd.gap_pct < 3.0
    assert Long01GapUpBreak().detect(sd, {}) == []


def test_long01_excludes_incomplete_first5():
    # 最初の5分足が不完全(1本だけ) → 除外(§4)
    rows = [bar(9, 0, 1030, 1035, 1030, 1032), bar(9, 6, 1038, 1045, 1037, 1044)]
    sd = make_symbol_day(bars1=rows, prev_close=1000.0)
    assert sd.first5_ok is False
    assert Long01GapUpBreak().detect(sd, {}) == []


def test_short01_fires_on_gap_and_lowbreak(first5_bars_1min):
    # GU+3%、first5_low=1030、09:06でlow1025割れ → 空売り
    rows = _gu_bars(first5_bars_1min, bar(9, 6, 1036, 1037, 1025, 1026))
    sd = make_symbol_day(bars1=rows, prev_close=1000.0)
    assert sd.first5_low == 1030
    sigs = Short01GapUpLow().detect(sd, {})
    assert len(sigs) == 1 and sigs[0].side == "SHORT"


def test_short05_fires_on_gapdown():
    # GD -3%: prev_close=1000, day_open=970 → gap=-3.0%。first5_low割れで空売り
    rows = [
        bar(9, 0, 970, 972, 965, 968), bar(9, 1, 968, 970, 963, 966),
        bar(9, 2, 966, 968, 962, 964), bar(9, 3, 964, 966, 961, 963),
        bar(9, 4, 963, 965, 960, 962),  # first5_low=960
        bar(9, 5, 962, 963, 961, 962),
        bar(9, 6, 961, 962, 955, 956),  # 960割れ
    ]
    sd = make_symbol_day(bars1=rows, prev_close=1000.0)
    assert abs(sd.gap_pct + 3.0) < 1e-6
    sigs = Short05GapDownLow().detect(sd, {})
    assert len(sigs) == 1 and sigs[0].side == "SHORT"
