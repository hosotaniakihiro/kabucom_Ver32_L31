"""scheduler_jobs/summary/push_bar_backfill の確認 (2026-10-26)。"""
import datetime as dt
import sqlite3

import pandas as pd

from scheduler_jobs.summary import push_bar_backfill as pbb

SCHEMA = """
CREATE TABLE stock_summary_1min (
    id INTEGER NOT NULL, symbol VARCHAR NOT NULL, symbolname VARCHAR, datetime DATETIME NOT NULL,
    date DATE NOT NULL, time_range VARCHAR NOT NULL, start_time TIME, end_time TIME, time TIME,
    source VARCHAR, interval INTEGER, open FLOAT, high FLOAT, low FLOAT, close FLOAT,
    open_price FLOAT, high_price FLOAT, low_price FLOAT, close_price FLOAT, volume FLOAT,
    rsi FLOAT, last_update DATETIME, score FLOAT, score_total FLOAT, final_score FLOAT,
    display_score FLOAT, score_buy FLOAT, score_sell FLOAT, buy_score FLOAT, sell_score FLOAT,
    technical_ready INTEGER, display_ready INTEGER, PRIMARY KEY (id), UNIQUE (symbol, datetime)
)
"""

NOW = dt.datetime(2026, 10, 26, 11, 0, 20)


def _bars():
    t = pd.Timestamp("2026-10-26 10:57:00")
    rows = []
    for sym, px in (("6525", 6600.0), ("6857", 20000.0), ("100A", 120.0)):
        for k in range(4):  # 10:57, 10:58, 10:59, 11:00 (11:00 は未確定)
            rows.append(dict(symbol=sym, symbolname=sym, datetime=t + pd.Timedelta(minutes=k),
                             open=px, high=px + 5, low=px - 5, close=px + k, volume=100.0 * (k + 1)))
    rows.append(dict(symbol="6525", symbolname="x", datetime=pd.Timestamp("2026-10-23 10:00"),
                     open=1, high=1, low=1, close=6500.0, volume=1))  # 前日の足
    return pd.DataFrame(rows)


def test_rows_keep_only_closed_today_and_price_range():
    rows = pbb.bars_to_rows(_bars(), now=NOW, since=None)
    keys = {(r[0], r[2]) for r in rows}
    assert keys == {("6525", "2026-10-26 10:57:00"), ("6525", "2026-10-26 10:58:00"), ("6525", "2026-10-26 10:59:00")}
    r = next(x for x in rows if x[2] == "2026-10-26 10:59:00")
    assert r[4] == "10:59-10:59" and r[8] == pbb.SOURCE and r[13] == 6602.0 and r[18] == 300.0


def test_since_limits_window():
    rows = pbb.bars_to_rows(_bars(), now=NOW, since=dt.datetime(2026, 10, 26, 10, 59))
    assert [(r[0], r[2]) for r in rows] == [("6525", "2026-10-26 10:59:00")]


def test_insert_missing_never_overwrites(tmp_path):
    db = tmp_path / "summary.db"
    con = sqlite3.connect(db)
    con.execute(SCHEMA)
    con.execute("INSERT INTO stock_summary_1min (symbol, datetime, date, time_range, source, close, rsi) "
                "VALUES ('6525', '2026-10-26 10:58:00', '2026-10-26', '10:58-10:58', 'summary_recovery_yahoo_1m', 1.0, 55.0)")
    con.commit(); con.close()
    rows = pbb.bars_to_rows(_bars(), now=NOW, since=None)
    assert pbb.insert_missing(str(db), rows) == 2
    assert pbb.insert_missing(str(db), rows) == 0  # 2 回目は何も入らない
    con = sqlite3.connect(db)
    got = con.execute("select datetime, source, close, rsi from stock_summary_1min order by datetime").fetchall()
    con.close()
    assert got[1] == ("2026-10-26 10:58:00", "summary_recovery_yahoo_1m", 1.0, 55.0)  # 既存行はそのまま
    assert [g[1] for g in got] == [pbb.SOURCE, "summary_recovery_yahoo_1m", pbb.SOURCE]


def test_disabled_by_env(monkeypatch):
    monkeypatch.setenv("SUMMARY_PUSH_BAR_BACKFILL_ENABLED", "0")
    assert pbb.backfill_from_bar_cache(1, NOW) == 0
    monkeypatch.setenv("SUMMARY_PUSH_BAR_BACKFILL_ENABLED", "1")
    assert pbb.backfill_from_bar_cache(3, NOW) == 0  # 1 分足以外は対象外
