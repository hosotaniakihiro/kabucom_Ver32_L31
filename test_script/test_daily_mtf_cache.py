"""日足MTFキャッシュ (stock_analysis_latest の読み直し削減)。

背景 (2026-08-18):
  日足は 1 日 1 回しか更新されないのに TTL が 300 秒だったため、
  5.17GB の stock_analysis.db から WHERE も LIMIT も無い SELECT で
  4,192 行を 1 日 70 回以上読み直していた (実測 1 回 24 秒)。

  TTL を延ばすだけだと日足バッチが走った直後に古い値を使い続けるので、
  DB ファイルの更新時刻が変わったら TTL 内でも読み直す。
"""
from __future__ import annotations

import pandas as pd
import pytest

import trading.summary.mtf.daily_mtf_summary as M


@pytest.fixture
def cache(monkeypatch):
    calls = {"n": 0}
    frames = [
        pd.DataFrame({"stock_code": ["1301"], "close": [100.0]}),
        pd.DataFrame({"stock_code": ["9999"], "close": [200.0]}),
    ]

    def fake():
        i = min(calls["n"], len(frames) - 1)
        calls["n"] += 1
        return frames[i]

    monkeypatch.setattr(M, "load_daily_mtf_latest_df", fake)
    mt = [1000.0]
    monkeypatch.setattr(M, "_daily_db_mtime", lambda: mt[0])
    M.reset_daily_cache()
    yield calls, mt
    M.reset_daily_cache()


def test_default_ttl_is_long():
    assert M._daily_cache_ttl_sec() >= 1800.0, "日足なのに TTL が短い"


def test_ttl_is_env_tunable(monkeypatch):
    monkeypatch.setenv("DAILY_MTF_CACHE_TTL_SEC", "60")
    assert M._daily_cache_ttl_sec() == 60.0


def test_within_ttl_does_not_reload(cache):
    calls, _ = cache
    a = M._get_cached_daily_df()
    b = M._get_cached_daily_df()
    assert calls["n"] == 1, "TTL 内なのに読み直している"
    assert a.equals(b)


def test_db_update_forces_reload_even_within_ttl(cache):
    """日足バッチが走ったら TTL 内でも読み直すこと。"""
    calls, mt = cache
    M._get_cached_daily_df()
    mt[0] = 2000.0
    got = M._get_cached_daily_df()
    assert calls["n"] == 2, "DB が更新されたのに古いキャッシュを返している"
    assert got["stock_code"].iloc[0] == "9999"


def test_reset_forces_reload(cache):
    calls, _ = cache
    M._get_cached_daily_df()
    M.reset_daily_cache()
    M._get_cached_daily_df()
    assert calls["n"] == 2


def test_missing_mtime_falls_back_to_ttl(monkeypatch):
    """mtime が取れない環境でも TTL だけで動くこと。"""
    calls = {"n": 0}

    def fake():
        calls["n"] += 1
        return pd.DataFrame({"stock_code": ["1301"]})

    monkeypatch.setattr(M, "load_daily_mtf_latest_df", fake)
    monkeypatch.setattr(M, "_daily_db_mtime", lambda: None)
    M.reset_daily_cache()
    try:
        M._get_cached_daily_df()
        M._get_cached_daily_df()
        assert calls["n"] == 1
    finally:
        M.reset_daily_cache()
