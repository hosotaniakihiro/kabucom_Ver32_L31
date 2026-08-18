"""ranking DB 読み出しの TTL キャッシュ。

背景 (2026-08-18):
  1 tick の中で _read_ranking_df_from_db が 5 回呼ばれ、そのたびに NAS 上の
  ranking DB へ SELECT * ... LIMIT 20000 を投げていた (実測 15回/7tick)。
  ranking_collector は 1 分に 1 回しか書かないので同じ tick 内で読み直しても
  内容は変わらない。実測 88 秒/tick のうち最大の繰り返しコストだった。

  本番実測: 5 回の読み出しが 21.29s -> 4.39s (4.9倍)、内容は完全一致。
"""
from __future__ import annotations

import pandas as pd
import pytest

import trading.summary.controller_enrich as CE


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    CE.reset_ranking_df_cache()
    monkeypatch.delenv("SUMMARY_ENRICH_RANKING_DB_CACHE_TTL_SEC", raising=False)
    yield
    CE.reset_ranking_df_cache()


def _stub(monkeypatch, frames):
    calls = {"n": 0}

    def fake(limit=20000):
        i = min(calls["n"], len(frames) - 1)
        calls["n"] += 1
        return frames[i]

    monkeypatch.setattr(CE, "_read_ranking_df_from_db_uncached", fake)
    return calls


def test_default_ttl_is_30s():
    assert CE._ranking_df_cache_ttl() == 30.0


def test_second_call_uses_cache(monkeypatch):
    a = pd.DataFrame({"symbol": ["1301"], "rank": [1]})
    b = pd.DataFrame({"symbol": ["9999"], "rank": [2]})
    calls = _stub(monkeypatch, [a, b])

    first = CE._read_ranking_df_from_db()
    second = CE._read_ranking_df_from_db()

    assert calls["n"] == 1, "DB を 2 回読んでいる"
    assert first.equals(second)
    assert second["symbol"].iloc[0] == "1301", "2 回目が別データになっている"


def test_ttl_zero_disables_cache(monkeypatch):
    monkeypatch.setenv("SUMMARY_ENRICH_RANKING_DB_CACHE_TTL_SEC", "0")
    a = pd.DataFrame({"symbol": ["1301"]})
    b = pd.DataFrame({"symbol": ["9999"]})
    calls = _stub(monkeypatch, [a, b])

    CE._read_ranking_df_from_db()
    second = CE._read_ranking_df_from_db()

    assert calls["n"] == 2, "TTL=0 でもキャッシュしている"
    assert second["symbol"].iloc[0] == "9999"


def test_reset_forces_reread(monkeypatch):
    a = pd.DataFrame({"symbol": ["1301"]})
    b = pd.DataFrame({"symbol": ["9999"]})
    calls = _stub(monkeypatch, [a, b])

    CE._read_ranking_df_from_db()
    CE.reset_ranking_df_cache()
    second = CE._read_ranking_df_from_db()

    assert calls["n"] == 2
    assert second["symbol"].iloc[0] == "9999"


def test_expired_ttl_rereads(monkeypatch):
    monkeypatch.setenv("SUMMARY_ENRICH_RANKING_DB_CACHE_TTL_SEC", "10")
    a = pd.DataFrame({"symbol": ["1301"]})
    b = pd.DataFrame({"symbol": ["9999"]})
    calls = _stub(monkeypatch, [a, b])

    t = [1000.0]
    monkeypatch.setattr(CE.time, "monotonic", lambda: t[0])
    CE._read_ranking_df_from_db()
    t[0] += 11.0                      # TTL 超過
    second = CE._read_ranking_df_from_db()

    assert calls["n"] == 2, "TTL 超過後も再読み込みしていない"
    assert second["symbol"].iloc[0] == "9999"


def test_cache_returns_copy_so_caller_cannot_corrupt_it(monkeypatch):
    a = pd.DataFrame({"symbol": ["1301"], "rank": [1]})
    _stub(monkeypatch, [a])

    first = CE._read_ranking_df_from_db()
    first.loc[0, "rank"] = 999
    second = CE._read_ranking_df_from_db()

    assert second.loc[0, "rank"] == 1, "呼び出し側の変更がキャッシュを壊している"
