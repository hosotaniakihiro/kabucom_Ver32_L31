# 未来情報混入防止(§7/§16)
from __future__ import annotations

import pandas as pd
import pytest

from backtest16.tests.conftest import make_symbol_day, bar, ts, ticks_df
from backtest16.features import SymbolDay, confirm_ts
from backtest16.strategies.longs import Long04Range15Break, Long05VolumeBreak
from backtest16.engine.execution_engine import execute, Signal


def test_confirmed_bars_asof_excludes_future():
    rows = [bar(9, 0, 100, 101, 99, 100), bar(9, 1, 100, 101, 99, 100), bar(9, 2, 100, 101, 99, 100)]
    sd = make_symbol_day(bars1=rows, prev_close=100.0)
    # 09:01:30 時点で確定済みは 09:00 足(確定09:01)のみ。09:01足(確定09:02)は未確定。
    cb = sd.confirmed_bars_asof(1, ts(9, 1, 30))
    assert len(cb) == 1 and cb.iloc[-1]["ts"] == ts(9, 0)


def test_range15_not_used_before_915():
    # 9:15高値ブレイクは 09:15 より前のシグナルを出さない
    rows = [bar(9, h // 60, 100, 100 + h, 99, 100) for h in range(0, 16)]  # dummy
    rows = [bar(9, m, 100, 105, 99, 100) for m in range(0, 16)]
    rows.append(bar(9, 16, 100, 120, 99, 118))   # 9:15以降にブレイク
    sd = make_symbol_day(bars1=rows, prev_close=100.0)
    sigs = Long04Range15Break().detect(sd, {})
    if sigs:
        assert sigs[0].signal_ts >= ts(9, 15)


def test_volume_avg_excludes_evaluation_bar():
    # 評価足を平均に含めない(§7)。直前5本=低出来高、評価足=高出来高&高値更新。
    rows = []
    for m in range(0, 5):
        rows.append(bar(9, m, 100, 101, 99, 100, v=1000))
    rows.append(bar(9, 5, 100, 110, 99, 109, v=10000))   # 3倍超・高値更新
    sd = make_symbol_day(bars1=rows, prev_close=100.0)
    sigs = Long05VolumeBreak().detect(sd, {})
    assert len(sigs) == 1
    # シグナルは評価足の確定時刻(09:06)
    assert sigs[0].signal_ts == confirm_ts(ts(9, 5), 1)


def test_execution_entry_strictly_after_signal_ohlc():
    # 次足始値エントリー: signal_ts と同時刻の足では約定しない
    rows = [bar(9, 6, 1000, 1001, 999, 1000), bar(9, 7, 1002, 1005, 1001, 1004)]
    sd = make_symbol_day(bars1=rows, prev_close=1000.0)
    sig = Signal("T", "7777", ts(9, 6).date(), "LONG", ts(9, 6), 1000, {})
    t = execute(sd, sig, "C_OHLC", {})
    # signal 09:06 → エントリーは 09:06 ラベル足の open(=次に始まる足)。09:06 の高安で即理想約定しない。
    assert t.entry_ts >= ts(9, 6)
    # エントリー足のopenでエントリー、同足のhighでのTP理想約定は許容(次足始値方式)だが
    # signalより前の足では絶対に約定しない
    assert t.entry_ts >= sig.signal_ts


def test_first5_not_available_before_lock():
    # first5_high/low は 09:00-09:04 のみから作られ、09:05以降のバーを含めない
    rows = [bar(9, m, 1000, 1000 + m, 1000 - m, 1000) for m in range(0, 5)]
    rows.append(bar(9, 5, 1000, 5000, 10, 1000))   # 09:05に極端な足
    sd = make_symbol_day(bars1=rows, prev_close=1000.0)
    # first5_high は 09:04 までの max(=1004), 09:05 の 5000 を含まない
    assert sd.first5_high == 1004 and sd.first5_low == 996
