# -*- coding: utf-8 -*-
"""P0-9: Ranking Entry technical_attach 30 秒の修正を検証する。

対象:
  trading/ranking/entry_from_ranking.py
      _ranking_entry_fix_get_gc_history           symbols で絞ってから copy
      _ranking_entry_fix_latest_summary_tech_map  symbol -> latest technical cache
  trading/ranking/ranking_technical_store.py
      _read_latest_tech_from_db                   接続 leak と table_missing 負キャッシュ

検証方針:
  - cache が効いても **返る technical が build と一致する**こと (痩せさせない)
  - 部分ヒットでは cache を使わない (欠けた銘柄の technical を捨てない)
  - 接続が leak しない (outstanding == 0)
  - table_missing は恒久 skip ではなく TTL 後に再確認する
"""
import os
import sys
import time
import types
import sqlite3
import tempfile
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import trading.ranking.entry_from_ranking as efr
import trading.ranking.ranking_technical_store as rts
from database import sqlite_util


SYMS = [str(1400 + i) for i in range(7)]


def _make_history(n_symbols, bars_per_symbol):
    base = pd.Timestamp("2026-08-27 09:00:00")
    rows = []
    for i in range(n_symbols):
        sym = str(1400 + i)
        for b in range(bars_per_symbol):
            rows.append({
                "symbol": sym,
                "datetime": base + pd.Timedelta(minutes=b),
                "close": 1000.0 + b,
                "macd": 0.5 + b * 0.01,
                "signal": 0.4,
                "rsi": 55.0,
                "slope": 0.2,
                "slope_atr_scaled": 0.3,
                "score_total": 12.0 + i,
                "hist": 0.1,
            })
    return pd.DataFrame(rows)


class _FakeGC:
    """get_summary_history の呼び出し回数を数える stub。"""

    def __init__(self, df):
        self.df = df
        self.calls = 0

    def get_summary_history(self, tf, source=None):
        self.calls += 1
        return self.df


@pytest.fixture
def gc_stub(monkeypatch):
    df = _make_history(330, 67)          # 実測に近い 22,110 行
    fake = _FakeGC(df)

    mod = types.ModuleType("core.global_context.context")
    mod.global_context = fake
    monkeypatch.setitem(sys.modules, "core.global_context.context", mod)

    efr._RANKING_TECH_CACHE.clear()
    monkeypatch.setenv("RANKING_ENTRY_TECH_FALLBACK_CACHE_TTL_SEC", "30")
    yield fake
    efr._RANKING_TECH_CACHE.clear()


# ------------------------------------------------------------------
# 1. history を symbols で絞ってから copy する
# ------------------------------------------------------------------
def test_history_filtered_by_symbols(gc_stub):
    out = efr._ranking_entry_fix_get_gc_history(1, symbols=set(SYMS))
    assert not out.empty
    assert set(out["symbol"].astype(str)) == set(SYMS)
    assert len(out) < len(gc_stub.df)


def test_history_without_symbols_returns_all(gc_stub):
    out = efr._ranking_entry_fix_get_gc_history(1)
    assert len(out) == len(gc_stub.df)


def test_history_copy_is_not_a_view(gc_stub):
    out = efr._ranking_entry_fix_get_gc_history(1, symbols=set(SYMS))
    out.loc[out.index[0], "close"] = -1.0
    assert (gc_stub.df["close"] != -1.0).all()


# ------------------------------------------------------------------
# 2. tech map の中身
# ------------------------------------------------------------------
def test_tech_map_builds_for_requested_symbols(gc_stub):
    m = efr._ranking_entry_fix_latest_summary_tech_map(set(SYMS))
    assert set(m) == set(SYMS)
    for sym, tech in m.items():
        assert tech["ranking_tech_db"] == "summary_history_cache"
        assert tech["ranking_tech_ready"] == 1
        assert tech["macd_hist"] == pytest.approx(0.1)


def test_tech_map_uses_latest_bar(gc_stub):
    m = efr._ranking_entry_fix_latest_summary_tech_map({"1400"})
    # bars_per_symbol=67 -> 最終足は 09:00 + 66 分
    assert m["1400"]["ranking_tech_datetime"] == pd.Timestamp("2026-08-27 10:06:00")


# ------------------------------------------------------------------
# 3. cache
# ------------------------------------------------------------------
def test_cache_hit_avoids_history_get(gc_stub):
    efr._ranking_entry_fix_latest_summary_tech_map(set(SYMS))
    first = gc_stub.calls
    assert first == 3                      # tf=1/3/5

    m2 = efr._ranking_entry_fix_latest_summary_tech_map(set(SYMS))
    assert gc_stub.calls == first          # history を引き直さない
    assert set(m2) == set(SYMS)


def test_cache_result_equals_fresh_build(gc_stub):
    fresh = efr._ranking_entry_fix_latest_summary_tech_map(set(SYMS))
    cached = efr._ranking_entry_fix_latest_summary_tech_map(set(SYMS))
    assert cached == fresh                 # 痩せていない


def test_partial_hit_rebuilds(gc_stub):
    efr._ranking_entry_fix_latest_summary_tech_map({"1400", "1401"})
    calls = gc_stub.calls
    # 1 銘柄でも欠けていれば cache を使わず作り直す
    m = efr._ranking_entry_fix_latest_summary_tech_map({"1400", "1402"})
    assert gc_stub.calls > calls
    assert set(m) == {"1400", "1402"}


def test_cache_ttl_zero_disables_cache(gc_stub, monkeypatch):
    monkeypatch.setenv("RANKING_ENTRY_TECH_FALLBACK_CACHE_TTL_SEC", "0")
    efr._ranking_entry_fix_latest_summary_tech_map(set(SYMS))
    calls = gc_stub.calls
    efr._ranking_entry_fix_latest_summary_tech_map(set(SYMS))
    assert gc_stub.calls == calls * 2


def test_cache_expires(gc_stub, monkeypatch):
    monkeypatch.setenv("RANKING_ENTRY_TECH_FALLBACK_CACHE_TTL_SEC", "0.2")
    efr._ranking_entry_fix_latest_summary_tech_map(set(SYMS))
    calls = gc_stub.calls
    time.sleep(0.3)
    efr._ranking_entry_fix_latest_summary_tech_map(set(SYMS))
    assert gc_stub.calls > calls


# ------------------------------------------------------------------
# 4. 速度 (実測を残す)
# ------------------------------------------------------------------
def test_cached_call_is_much_faster(gc_stub, capsys):
    t0 = time.perf_counter()
    efr._ranking_entry_fix_latest_summary_tech_map(set(SYMS))
    build = time.perf_counter() - t0

    t1 = time.perf_counter()
    efr._ranking_entry_fix_latest_summary_tech_map(set(SYMS))
    cached = time.perf_counter() - t1

    with capsys.disabled():
        print("\n[P0-9] build=%.1fms cached=%.1fms speedup=%.1fx rows=%d" % (
            build * 1000, cached * 1000, build / max(cached, 1e-9), len(gc_stub.df)))
    assert cached < build
    assert cached < 0.2          # 目標: technical_attach < 0.2s


# ------------------------------------------------------------------
# 5. ranking_technical_store: 接続 leak と table_missing 負キャッシュ
# ------------------------------------------------------------------
@pytest.fixture
def tmp_ranking_db():
    d = tempfile.mkdtemp(prefix="p09_")
    path = str(Path(d) / "ranking_test.db")
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE dummy(x INTEGER)")
    con.commit()
    con.close()
    rts._TECH_TABLE_MISSING_AT.clear()
    yield path
    rts._TECH_TABLE_MISSING_AT.clear()


def test_read_latest_tech_does_not_leak_connections(tmp_ranking_db):
    sqlite_util.reset_stats()
    rts._read_latest_tech_from_db(tmp_ranking_db, SYMS)
    st = sqlite_util.stats()
    assert st["opened"] >= 1
    assert st["outstanding"] == 0, st


def test_table_missing_is_cached_but_reprobed(tmp_ranking_db, monkeypatch):
    monkeypatch.setenv("RANKING_ENTRY_TECH_TABLE_PROBE_TTL_SEC", "0.3")
    sqlite_util.reset_stats()

    assert rts._read_latest_tech_from_db(tmp_ranking_db, SYMS) == {}
    opened_1 = sqlite_util.stats()["opened"]
    assert opened_1 >= 1

    # TTL 内: DB を開かない
    assert rts._read_latest_tech_from_db(tmp_ranking_db, SYMS) == {}
    assert sqlite_util.stats()["opened"] == opened_1

    # TTL 経過後: もう一度確かめる (恒久 skip にしない)
    time.sleep(0.4)
    assert rts._read_latest_tech_from_db(tmp_ranking_db, SYMS) == {}
    assert sqlite_util.stats()["opened"] > opened_1
    assert sqlite_util.stats()["outstanding"] == 0


def test_table_probe_ttl_zero_always_probes(tmp_ranking_db, monkeypatch):
    monkeypatch.setenv("RANKING_ENTRY_TECH_TABLE_PROBE_TTL_SEC", "0")
    sqlite_util.reset_stats()
    rts._read_latest_tech_from_db(tmp_ranking_db, SYMS)
    a = sqlite_util.stats()["opened"]
    rts._read_latest_tech_from_db(tmp_ranking_db, SYMS)
    assert sqlite_util.stats()["opened"] > a


def test_existing_table_is_read_and_marks_present(tmp_ranking_db):
    con = sqlite3.connect(tmp_ranking_db)
    con.execute("CREATE TABLE %s (symbol TEXT, datetime TEXT, rsi REAL, macd REAL)" % rts.TABLE_NAME)
    con.executemany(
        "INSERT INTO %s VALUES (?,?,?,?)" % rts.TABLE_NAME,
        [("1400", "2026-08-27 09:00:00", 50.0, 0.1),
         ("1400", "2026-08-27 09:01:00", 55.0, 0.2),
         ("1401", "2026-08-27 09:01:00", 60.0, 0.3)],
    )
    con.commit()
    con.close()

    sqlite_util.reset_stats()
    out = rts._read_latest_tech_from_db(tmp_ranking_db, ["1400", "1401"])
    assert set(out) == {"1400", "1401"}
    assert out["1400"]["rsi"] == 55.0        # 最新足
    assert tmp_ranking_db not in rts._TECH_TABLE_MISSING_AT
    assert sqlite_util.stats()["outstanding"] == 0


def test_readonly_connect_does_not_create_db():
    missing = str(Path(tempfile.mkdtemp(prefix="p09_")) / "nope.db")
    rts._TECH_TABLE_MISSING_AT.clear()
    with pytest.raises(FileNotFoundError):
        with sqlite_util.readonly_connect(missing, timeout=1.0):
            pass
    assert not os.path.exists(missing)
