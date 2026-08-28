# -*- coding: utf-8 -*-
"""P0-9: Ranking Entry の ranking_source (NAS ranking DB fallback) の計測と修正。

実測 (2026-08-27 console_20260827_125946_75344.log):

    [RANKING ENTRY SOURCE DB FALLBACK] probe_elapsed=  n=38 median=4.54s sum=253.9s
      うち MAX(datetime) の SQL 合計は 21.0s
      -> 232.9s (92%) は connect / sqlite_master / PRAGMA table_info の往復

ここで検証するのは:
  - スキーマキャッシュが metadata 往復を消すこと
  - **恒久 skip ではない**こと (TTL 経過後は問い合わせ直す)
  - 接続が leak しないこと
  - db_path の束ねが return / 例外でも必ず解けること
"""
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import trading.ranking.entry_from_ranking as efr
from database import sqlite_util


class _CountingConn:
    """execute の回数を数える薄い proxy。"""

    def __init__(self, conn):
        self._conn = conn
        self.calls = []

    def execute(self, sql, *a, **kw):
        self.calls.append(str(sql))
        return self._conn.execute(sql, *a, **kw)

    def n(self, needle):
        return sum(1 for s in self.calls if needle in s)


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "ranking20260827.db")
    con = sqlite3.connect(path)
    for t in ("ranking_snapshot_1min", "ranking_raw_1min", "ranking_summary_1min"):
        con.execute(
            'CREATE TABLE "%s" (symbol TEXT, datetime TEXT, price REAL, rank_position INTEGER)' % t)
        con.executemany(
            'INSERT INTO "%s" VALUES (?,?,?,?)' % t,
            [("1400", "2026-08-27 13:00:00", 100.0, 1),
             ("1401", "2026-08-27 13:01:00", 200.0, 2)],
        )
    con.commit()
    con.close()
    efr._RSRC_SCHEMA_CACHE.clear()
    yield path
    efr._RSRC_SCHEMA_CACHE.clear()


# ------------------------------------------------------------------
# table_info のキャッシュ
# ------------------------------------------------------------------
def test_table_columns_cached(db, monkeypatch):
    monkeypatch.setenv("RANKING_ENTRY_SOURCE_DB_SCHEMA_TTL_SEC", "300")
    with sqlite_util.readonly_connect(db, timeout=2.0) as raw, efr._rsrc_bind_db_path(db):
        c = _CountingConn(raw)
        cols1 = efr._rsrc_table_columns(c, "ranking_snapshot_1min")
        assert cols1 == ["symbol", "datetime", "price", "rank_position"]
        assert c.n("table_info") == 1

        cols2 = efr._rsrc_table_columns(c, "ranking_snapshot_1min")
        assert cols2 == cols1
        assert c.n("table_info") == 1          # 2 回目は往復しない


def test_table_columns_ttl_reprobes(db, monkeypatch):
    monkeypatch.setenv("RANKING_ENTRY_SOURCE_DB_SCHEMA_TTL_SEC", "0.3")
    with sqlite_util.readonly_connect(db, timeout=2.0) as raw, efr._rsrc_bind_db_path(db):
        c = _CountingConn(raw)
        efr._rsrc_table_columns(c, "ranking_snapshot_1min")
        assert c.n("table_info") == 1
        time.sleep(0.4)
        efr._rsrc_table_columns(c, "ranking_snapshot_1min")
        assert c.n("table_info") == 2          # 恒久 skip ではない


def test_schema_ttl_zero_disables_cache(db, monkeypatch):
    monkeypatch.setenv("RANKING_ENTRY_SOURCE_DB_SCHEMA_TTL_SEC", "0")
    with sqlite_util.readonly_connect(db, timeout=2.0) as raw, efr._rsrc_bind_db_path(db):
        c = _CountingConn(raw)
        efr._rsrc_table_columns(c, "ranking_snapshot_1min")
        efr._rsrc_table_columns(c, "ranking_snapshot_1min")
        assert c.n("table_info") == 2


def test_unbound_db_path_still_works(db):
    """db_path を束ねていなくても動く (キャッシュしないだけ)。"""
    efr._RSRC_SCHEMA_CACHE.clear()
    assert efr._rsrc_current_db_path() == ""
    with sqlite_util.readonly_connect(db, timeout=2.0) as raw:
        c = _CountingConn(raw)
        assert efr._rsrc_table_columns(c, "ranking_snapshot_1min")
        assert efr._rsrc_table_columns(c, "ranking_snapshot_1min")
        assert c.n("table_info") == 2
    assert efr._RSRC_SCHEMA_CACHE == {}


# ------------------------------------------------------------------
# db_path の束ねは return / 例外でも必ず解ける
# ------------------------------------------------------------------
def test_bind_restores_on_exception(db):
    assert efr._rsrc_current_db_path() == ""
    with pytest.raises(RuntimeError):
        with efr._rsrc_bind_db_path(db):
            assert efr._rsrc_current_db_path() == db
            raise RuntimeError("boom")
    assert efr._rsrc_current_db_path() == ""


def test_bind_restores_on_return(db):
    def _inner():
        with efr._rsrc_bind_db_path(db):
            return efr._rsrc_current_db_path()

    assert _inner() == db
    assert efr._rsrc_current_db_path() == ""


def test_bind_nests(db):
    with efr._rsrc_bind_db_path("outer.db"):
        with efr._rsrc_bind_db_path(db):
            assert efr._rsrc_current_db_path() == db
        assert efr._rsrc_current_db_path() == "outer.db"
    assert efr._rsrc_current_db_path() == ""


# ------------------------------------------------------------------
# candidate_tables のキャッシュ
# ------------------------------------------------------------------
def test_candidate_tables_cached(db, monkeypatch):
    monkeypatch.setenv("RANKING_ENTRY_SOURCE_DB_SCHEMA_TTL_SEC", "300")
    monkeypatch.delenv("RANKING_ENTRY_SOURCE_DB_TABLES", raising=False)
    with sqlite_util.readonly_connect(db, timeout=2.0) as raw, efr._rsrc_bind_db_path(db):
        c = _CountingConn(raw)
        t1 = efr._rsrc_candidate_tables(c)
        assert t1 == ["ranking_snapshot_1min", "ranking_raw_1min", "ranking_summary_1min"]
        probes = c.n("sqlite_master")
        assert probes == 6                 # 候補 6 件を実際に確かめた

        t2 = efr._rsrc_candidate_tables(c)
        assert t2 == t1
        assert c.n("sqlite_master") == probes   # 2 回目は往復しない


def test_candidate_tables_ttl_reprobes(db, monkeypatch):
    monkeypatch.setenv("RANKING_ENTRY_SOURCE_DB_SCHEMA_TTL_SEC", "0.3")
    with sqlite_util.readonly_connect(db, timeout=2.0) as raw, efr._rsrc_bind_db_path(db):
        c = _CountingConn(raw)
        efr._rsrc_candidate_tables(c)
        n1 = c.n("sqlite_master")
        time.sleep(0.4)
        efr._rsrc_candidate_tables(c)
        assert c.n("sqlite_master") > n1


# ------------------------------------------------------------------
# 実経路: leak しない / 結果が変わらない
# ------------------------------------------------------------------
def _run_core(db, monkeypatch):
    monkeypatch.setenv("RANKING_ENTRY_SOURCE_DB_FALLBACK_ENABLED", "1")
    monkeypatch.setenv("RANKING_DB_PATH", db)
    monkeypatch.setenv("RANKING_ENTRY_SOURCE_DB_MAX_AGE_SEC", "999999")
    monkeypatch.setenv("RANKING_ENTRY_SOURCE_DB_CACHE_TTL_SEC", "0")
    monkeypatch.setattr(efr, "_rsrc_resolve_db_path", lambda: db)
    return efr._rsrc_load_from_ranking_db_core()


def test_core_reads_and_does_not_leak(db, monkeypatch):
    sqlite_util.reset_stats()
    out = _run_core(db, monkeypatch)
    assert isinstance(out, pd.DataFrame)
    assert not out.empty
    assert set(out["symbol"].astype(str)) == {"1400", "1401"}
    st = sqlite_util.stats()
    assert st["opened"] >= 1
    assert st["outstanding"] == 0, st


def test_core_second_run_uses_schema_cache(db, monkeypatch):
    monkeypatch.setenv("RANKING_ENTRY_SOURCE_DB_SCHEMA_TTL_SEC", "300")
    efr._RSRC_SCHEMA_CACHE.clear()
    _run_core(db, monkeypatch)
    assert db in efr._RSRC_SCHEMA_CACHE
    ent = efr._RSRC_SCHEMA_CACHE[db]
    assert ent["tables"]
    assert "ranking_snapshot_1min" in ent["cols"]

    out2 = _run_core(db, monkeypatch)
    assert not out2.empty            # キャッシュ後も同じ結果


def test_core_unbinds_db_path(db, monkeypatch):
    _run_core(db, monkeypatch)
    assert efr._rsrc_current_db_path() == ""


def test_missing_db_is_not_created(monkeypatch, tmp_path):
    missing = str(tmp_path / "nope.db")
    monkeypatch.setattr(efr, "_rsrc_resolve_db_path", lambda: missing)
    out = efr._rsrc_load_from_ranking_db_core()
    assert out.empty
    assert not Path(missing).exists()
