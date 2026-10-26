# 2026-10-22: 同じ日に前回の決済より不利な所で入り直さない (追いかけ買いの停止)
import datetime as dt
import sqlite3

import pytest

from trading.handlers import entry_order_builder as EOB
from trading.position import positions_db_location as PDL

TODAY = dt.date.today().strftime("%Y-%m-%d")


@pytest.fixture
def posdb(tmp_path, monkeypatch):
    db = tmp_path / "positions.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE positions (symbol TEXT, side TEXT, status TEXT, avg_price REAL, exit_price REAL, exit_time TEXT)")
    con.commit()
    monkeypatch.setattr(PDL, "positions_db_path", lambda: db)

    def add(symbol, side, exit_price, exit_time, status="CLOSED"):
        c = sqlite3.connect(db)
        c.execute("INSERT INTO positions VALUES (?,?,?,?,?,?)", (symbol, side, status, 1.0, exit_price, exit_time))
        c.commit()
        c.close()

    con.close()
    return add


def guard(symbol, side, close):
    return EOB._reentry_chase_guard(symbol=symbol, side=side, source="SUMMARY_AI", entry_row={"close": close})


def test_436a_rebuy_above_last_exit_is_blocked(posdb):
    posdb("436A", "BUY_CREDIT", 1951.0, f"{TODAY} 13:55:25.730")
    ng = guard("436A", "BUY", 1981.0)                       # +1.54% 上で買い直し
    assert ng is not None and ng["reason"] == "REENTRY_CHASE"
    assert guard("436A", "BUY", 1960.0) is None             # +0.46% までは通す
    assert guard("436A", "BUY", 1940.0) is None             # 決済より安いのは通す


def test_uses_latest_exit_same_side_and_today_only(posdb):
    posdb("436A", "BUY_CREDIT", 1900.0, f"{TODAY} 10:00:00")
    posdb("436A", "BUY_CREDIT", 1990.0, f"{TODAY} 14:00:00")     # 最後の決済が基準
    posdb("436A", "SELL_CREDIT", 1700.0, f"{TODAY} 14:30:00")    # 逆方向は見ない
    posdb("7203", "BUY_CREDIT", 1000.0, "2026-01-05 10:00:00")    # 前日以前は見ない
    assert guard("436A", "BUY", 1995.0) is None
    assert guard("7203", "BUY", 2000.0) is None


def test_sell_is_mirrored(posdb):
    posdb("3697", "SELL_CREDIT", 1000.0, f"{TODAY} 10:00:00")
    assert guard("3697", "SELL", 990.0) is not None         # 1% 下で売り直し = 不利
    assert guard("3697", "SELL", 1010.0) is None


def test_disabled_and_fail_open(posdb, monkeypatch):
    posdb("436A", "BUY_CREDIT", 1951.0, f"{TODAY} 13:55:25")
    monkeypatch.setenv("ENTRY_REENTRY_CHASE_MAX_PCT", "0")
    assert guard("436A", "BUY", 1981.0) is None
    monkeypatch.delenv("ENTRY_REENTRY_CHASE_MAX_PCT")
    assert EOB._reentry_chase_guard(symbol="436A", side="BUY", source="X", entry_row={}) is None   # 判定価格なし
    monkeypatch.setattr(PDL, "positions_db_path", lambda: "C:/no/such/positions.db")
    assert guard("436A", "BUY", 1981.0) is None
