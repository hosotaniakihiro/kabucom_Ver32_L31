"""日足が古いまま判定に使われていないか監視する。

背景 (2026-08-18):
  日足DB が 2026-07-17 を最後に更新されておらず (夜間バッチが Norton の
  TLS 傍受で失敗し、かつタスクスケジューラにも未登録だった)、
  日足MTF が 1 か月前の値で売買判定に使われていた。

  それでもログは INFO の "latest_date=2026-07-16" だけで、
  古いことが分かる出力が無かったため気付けなかった。
"""
from __future__ import annotations

import logging

import pandas as pd
import pytest

import trading.summary.mtf.daily_mtf_loader as L


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("DAILY_MTF_STALE_WARN_DAYS", raising=False)
    yield


def _warnings(caplog, latest):
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=L.logger.name):
        L._warn_if_stale(latest, "test.db")
    return [r.getMessage() for r in caplog.records]


def test_default_threshold_allows_weekend_gap():
    assert L._stale_days_threshold() == 4, "連休を挟むと数日空くので閾値は 4 日"


def test_threshold_is_env_tunable(monkeypatch):
    monkeypatch.setenv("DAILY_MTF_STALE_WARN_DAYS", "10")
    assert L._stale_days_threshold() == 10


def test_month_old_data_warns(caplog):
    old = (pd.Timestamp.now().normalize() - pd.Timedelta(days=34)).date()
    msgs = _warnings(caplog, str(old))
    assert any("DAILY MTF STALE" in m for m in msgs), "1か月古いのに警告が出ない"
    assert any("34" in m for m in msgs)


def test_fresh_data_is_silent(caplog):
    fresh = (pd.Timestamp.now().normalize() - pd.Timedelta(days=1)).date()
    msgs = _warnings(caplog, str(fresh))
    assert not [m for m in msgs if "DAILY MTF STALE" in m], "新しいのに警告が出ている"


def test_weekend_gap_is_silent(caplog):
    """金曜のデータを月曜に読む (3日前) 程度では警告しない。"""
    d = (pd.Timestamp.now().normalize() - pd.Timedelta(days=3)).date()
    msgs = _warnings(caplog, str(d))
    assert not [m for m in msgs if "DAILY MTF STALE" in m]


def test_missing_date_warns(caplog):
    msgs = _warnings(caplog, None)
    assert any("latest_date unknown" in m for m in msgs)


def test_unparsable_date_warns(caplog):
    msgs = _warnings(caplog, "not-a-date")
    assert any("unparsable" in m for m in msgs)


def test_never_raises_on_bad_input():
    """監視のための処理でロード全体を壊さないこと。"""
    L._warn_if_stale(object(), "test.db")
