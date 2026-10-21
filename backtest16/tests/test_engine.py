# 約定/コスト/空売り規制/ポートフォリオ/欠損(§16)
from __future__ import annotations

import datetime as dt
import pandas as pd
import pytest

from backtest16.tests.conftest import make_symbol_day, bar, ts, ticks_df
from backtest16.engine.execution_engine import execute, Signal
from backtest16 import cost_model as CM
from backtest16 import short_sale_rules as SR
from backtest16 import borrow_availability as BA
from backtest16.engine import portfolio_engine as PE
from backtest16 import config as C


def _sig(side, h, m, ref=1000.0):
    return Signal("T", "7777", ts(9, 0).date(), side, ts(h, m), ref, {})


# ---------- EXECUTION_C ----------
def test_ohlc_long_takeprofit():
    # entry 次足09:07 open=1000、09:08でhigh>=1003(+0.3%) → TP
    rows = [bar(9, 7, 1000, 1001, 999, 1000), bar(9, 8, 1000, 1004, 999, 1002)]
    sd = make_symbol_day(bars1=rows, prev_close=1000.0)
    t = execute(sd, _sig("LONG", 9, 6), "C_OHLC", {})
    assert t.filled and t.exit_reason == "TP"
    assert t.exit_price == pytest.approx(1003.0)   # 1000*1.003
    assert t.gross_pnl > 0


def test_ohlc_same_bar_loss_first():
    # 同一足で TP(+0.3%)とSL(-0.3%)両到達 → 保守的に損切り優先(§8)
    rows = [bar(9, 7, 1000, 1001, 999, 1000), bar(9, 8, 1000, 1010, 990, 1000)]
    sd = make_symbol_day(bars1=rows, prev_close=1000.0)
    t = execute(sd, _sig("LONG", 9, 6), "C_OHLC", {})
    assert t.exit_reason == "SL"
    assert t.exit_price == pytest.approx(997.0)    # 1000*0.997


def test_ohlc_short_tp_is_down():
    # 空売り: 利確は下方向。entry09:07 open=1000、09:08 low<=997 → TP
    rows = [bar(9, 7, 1000, 1001, 999, 1000), bar(9, 8, 1000, 1001, 996, 998)]
    sd = make_symbol_day(bars1=rows, prev_close=1000.0)
    t = execute(sd, _sig("SHORT", 9, 6), "C_OHLC", {})
    assert t.exit_reason == "TP" and t.exit_price == pytest.approx(997.0)
    assert t.gross_pnl > 0   # (entry-exit)*qty


def test_ohlc_time_exit():
    # TP/SLに当たらず300秒(5本)でTIME決済
    rows = [bar(9, 7 + i, 1000, 1001, 999, 1000) for i in range(6)]
    sd = make_symbol_day(bars1=rows, prev_close=1000.0)
    t = execute(sd, _sig("LONG", 9, 6), "C_OHLC", {})
    assert t.exit_reason in ("TIME", "EOD")
    assert t.hold_seconds <= C.MAX_HOLD_SECONDS + 60


def test_qty_cap_and_too_expensive():
    rows = [bar(9, 7, 1000, 1001, 999, 1000), bar(9, 8, 1000, 1004, 999, 1002)]
    sd = make_symbol_day(bars1=rows, prev_close=1000.0)
    t = execute(sd, _sig("LONG", 9, 6), "C_OHLC", {})
    assert t.qty * t.entry_price <= C.PER_SYMBOL_NOTIONAL_YEN  # 70万以下
    assert t.qty % 100 == 0
    # 高額銘柄 → 1単元>70万 → TOO_EXPENSIVE
    rows2 = [bar(9, 7, 8000, 8001, 7999, 8000), bar(9, 8, 8000, 8040, 7999, 8020)]
    sd2 = make_symbol_day(bars1=rows2, prev_close=8000.0)
    t2 = execute(sd2, _sig("LONG", 9, 6, ref=8000), "C_OHLC", {})
    assert t2.exit_reason == "TOO_EXPENSIVE" and not t2.filled


def test_missing_data_no_fabricated_fill():
    sd = make_symbol_day(bars1=[], prev_close=1000.0)
    t = execute(sd, _sig("LONG", 9, 6), "C_OHLC", {})
    assert not t.filled and t.exit_reason == "DATA_GAP" and t.net_pnl == 0.0


def test_push_nofill_when_no_ticks():
    rows = [bar(9, 7, 1000, 1001, 999, 1000)]
    sd = make_symbol_day(bars1=rows, prev_close=1000.0)
    t = execute(sd, _sig("LONG", 9, 6), "B_PUSH", {})
    assert not t.filled and t.exit_reason == "NOFILL"


def test_push_tick_entry_after_signal():
    # エントリーは signal_ts より後のティック(同一イベント約定禁止 §7)
    tk = ticks_df([(9, 6, 0, 1000), (9, 6, 30, 1000), (9, 7, 0, 1004)])
    rows = [bar(9, 7, 1000, 1004, 999, 1002)]
    sd = make_symbol_day(bars1=rows, ticks=tk, prev_close=1000.0)
    t = execute(sd, _sig("LONG", 9, 6), "B_PUSH", {})
    assert t.filled and t.entry_ts > ts(9, 6)   # 厳密に後


# ---------- コストモデル ----------
def test_cost_short_lending_nonzero_even_intraday():
    r = CM.compute_costs(side="SHORT", entry_price=1000, exit_price=997, qty=1000, hold_days=1)
    assert r.lending_fee > 0            # 5分保有でも0にしない(§6.3)
    assert r.negative_rate_known is False
    assert r.margin_interest == 0.0     # 空売りに買方金利は付かない


def test_cost_long_vs_short_pnl_formula():
    assert CM.gross_pnl("LONG", 1000, 1003, 100) == pytest.approx(300)
    assert CM.gross_pnl("SHORT", 1000, 997, 100) == pytest.approx(300)
    net, gross, costs = CM.net_pnl("SHORT", 1000, 997, 100)
    assert gross == pytest.approx(300) and net < gross   # コスト差引


# ---------- 空売り規制・約定方向 ----------
def test_price_regulation_not_blanket_ban():
    # -10%未満 → 発動していない見込み
    s = SR.price_regulation_status(signal_ts=ts(10, 0), base_price=1000,
                                   intraday_low_before_signal=950)
    assert s.state == "NOT_TRIGGERED"
    # -10%以上 → 一律禁止ではなく「不確定」
    s2 = SR.price_regulation_status(signal_ts=ts(10, 0), base_price=1000,
                                    intraday_low_before_signal=890)
    assert s2.state == "UNDETERMINED"
    # 基準不明 → 確定しない
    s3 = SR.price_regulation_status(signal_ts=ts(10, 0), base_price=None,
                                    intraday_low_before_signal=None)
    assert s3.state == "UNKNOWN"


def test_board_fill_directions():
    row = pd.Series({"bid1": 999, "ask1": 1001})
    assert SR.short_fill_price_from_board(row) == 999   # 売り=買気配
    assert SR.cover_fill_price_from_board(row) == 1001  # 買い戻し=売気配
    assert SR.long_buy_fill_price_from_board(row) == 1001
    assert SR.long_sell_fill_price_from_board(row) == 999


def test_borrow_class_a_excluded():
    assert BA.is_executable_class("A") is True
    for k in ("B", "C", "D"):
        assert BA.is_executable_class(k) is False
    assert BA.classify("7777", ts(9, 0).date(), short_sellable_hint=True) == "B"
    assert BA.classify("7777", ts(9, 0).date(), short_sellable_hint=False) == "C"
    assert BA.classify("7777", ts(9, 0).date(), short_sellable_hint=None) in ("B", "C", "D")


# ---------- ポートフォリオ ----------
def _tr(sid, side, sym, h1, m1, h2, m2, pnl):
    return {"strategy_id": sid, "symbol": sym, "side": side, "filled": True,
            "entry_ts": ts(h1, m1), "exit_ts": ts(h2, m2), "net_pnl": pnl, "date": "2026-06-16"}


def test_portfolio_max_concurrent_3():
    trades = [
        _tr("LONG_01", "LONG", "A", 9, 10, 9, 20, 100),
        _tr("LONG_04", "LONG", "B", 9, 11, 9, 20, 100),
        _tr("LONG_05", "LONG", "C", 9, 12, 9, 20, 100),
        _tr("SHORT_07", "SHORT", "D", 9, 13, 9, 20, 100),  # 4本目→満杯で見送り
    ]
    r = PE.simulate(trades, mode="both")
    assert r["taken"] == 3 and r["missed"] == 1
    assert r["missed_reasons"].get("MAX_CONCURRENT") == 1


def test_portfolio_no_both_sides_same_symbol():
    trades = [
        _tr("LONG_01", "LONG", "A", 9, 10, 9, 20, 100),
        _tr("SHORT_01", "SHORT", "A", 9, 11, 9, 20, 100),  # 同一銘柄逆サイド→見送り
    ]
    r = PE.simulate(trades, mode="both")
    assert r["taken"] == 1 and r["missed_reasons"].get("SAME_SYMBOL_OPPOSITE") == 1
