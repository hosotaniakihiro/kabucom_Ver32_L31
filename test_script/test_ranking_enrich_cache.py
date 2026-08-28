"""ranking DB 読み出しの TTL キャッシュ。

背景 (2026-08-18):
  1 tick の中で _read_ranking_df_from_db が 5 回呼ばれ、そのたびに NAS 上の
  ranking DB へ SELECT * ... LIMIT 20000 を投げていた (実測 15回/7tick)。
  ranking_collector は 1 分に 1 回しか書かないので同じ tick 内で読み直しても
  内容は変わらない。実測 88 秒/tick のうち最大の繰り返しコストだった。

  本番実測: 5 回の読み出しが 21.29s -> 4.39s (4.9倍)、内容は完全一致。
"""
from __future__ import annotations

import time as _time

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


def _wait_bg(timeout=5.0):
    """background refresh の終了を待つ。"""
    end = _time.time() + timeout
    while _time.time() < end:
        if not CE._ranking_bg_running():
            return True
        _time.sleep(0.02)
    return False


def test_expired_ttl_serves_stale_then_refreshes_in_background(monkeypatch):
    """TTL 超過でも hot path はブロックせず、background で読み直す (P0-2)。

    旧実装は TTL 超過のたびに NAS へ同期で
    SELECT * ... LIMIT 20000 (108 列) を投げており、
    SUMMARY SETTER ENRICH が 67.80s / 217.38s まで伸びる原因だった。
    """
    monkeypatch.setenv("SUMMARY_ENRICH_RANKING_DB_CACHE_TTL_SEC", "10")
    a = pd.DataFrame({"symbol": ["1301"]})
    b = pd.DataFrame({"symbol": ["9999"]})
    calls = _stub(monkeypatch, [a, b])

    t = [1000.0]
    monkeypatch.setattr(CE.time, "monotonic", lambda: t[0])
    CE._read_ranking_df_from_db()
    assert calls["n"] == 1

    t[0] += 11.0                      # TTL 超過
    second = CE._read_ranking_df_from_db()

    # hot path は待たずに stale をそのまま返す
    assert second["symbol"].iloc[0] == "1301", "hot path が同期で読み直している"

    # ただし読み直し自体は background で必ず行われる
    assert _wait_bg(), "background refresh が終わらない"
    assert calls["n"] == 2, "TTL 超過後に読み直していない"

    third = CE._read_ranking_df_from_db()
    assert third["symbol"].iloc[0] == "9999", "background の結果が反映されていない"


def test_bg_refresh_can_be_disabled(monkeypatch):
    """env で従来どおりの同期読みへ戻せる。"""
    monkeypatch.setenv("SUMMARY_ENRICH_RANKING_DB_CACHE_TTL_SEC", "10")
    monkeypatch.setenv("SUMMARY_ENRICH_RANKING_BG_REFRESH", "0")
    a = pd.DataFrame({"symbol": ["1301"]})
    b = pd.DataFrame({"symbol": ["9999"]})
    calls = _stub(monkeypatch, [a, b])

    t = [1000.0]
    monkeypatch.setattr(CE.time, "monotonic", lambda: t[0])
    CE._read_ranking_df_from_db()
    t[0] += 11.0
    second = CE._read_ranking_df_from_db()

    assert calls["n"] == 2, "OFF なのに同期で読み直していない"
    assert second["symbol"].iloc[0] == "9999"


def test_too_old_cache_forces_sync_read(monkeypatch):
    """stale をいつまでも使い続けない。上限を超えたら同期で読み直す。"""
    monkeypatch.setenv("SUMMARY_ENRICH_RANKING_DB_CACHE_TTL_SEC", "10")
    monkeypatch.setenv("SUMMARY_ENRICH_RANKING_STALE_MAX_SEC", "60")
    a = pd.DataFrame({"symbol": ["1301"]})
    b = pd.DataFrame({"symbol": ["9999"]})
    calls = _stub(monkeypatch, [a, b])

    t = [1000.0]
    monkeypatch.setattr(CE.time, "monotonic", lambda: t[0])
    CE._read_ranking_df_from_db()
    t[0] += 120.0                     # stale_max も超過
    second = CE._read_ranking_df_from_db()

    assert calls["n"] == 2, "古すぎるのに同期で読み直していない"
    assert second["symbol"].iloc[0] == "9999"


def test_empty_read_keeps_existing_cache(monkeypatch):
    """読めなかったときに既存キャッシュを空で潰さない。"""
    monkeypatch.setenv("SUMMARY_ENRICH_RANKING_DB_CACHE_TTL_SEC", "10")
    monkeypatch.setenv("SUMMARY_ENRICH_RANKING_STALE_MAX_SEC", "1")
    a = pd.DataFrame({"symbol": ["1301"]})
    calls = _stub(monkeypatch, [a, pd.DataFrame()])

    t = [1000.0]
    monkeypatch.setattr(CE.time, "monotonic", lambda: t[0])
    CE._read_ranking_df_from_db()
    t[0] += 120.0
    second = CE._read_ranking_df_from_db()

    assert calls["n"] == 2
    assert not second.empty, "空読みで既存キャッシュが潰れている"
    assert second["symbol"].iloc[0] == "1301"


def test_projection_keeps_needed_columns():
    """必要列だけを引く。別名も全部含める。"""
    cols = ["symbol", "datetime", "rank_type", "rank_position", "change_rate",
            "turnover", "trading_volume", "market", "unused_a", "unused_b"]
    sel = CE._ranking_select_columns(cols)
    assert sel != "*", "projection されていない"
    for c in ("symbol", "datetime", "rank_type", "rank_position",
              "change_rate", "turnover", "trading_volume", "market"):
        assert '"%s"' % c in sel, "必要列 %s が落ちている" % c
    assert "unused_a" not in sel, "不要列を引いている"


def test_projection_falls_back_to_star_when_nothing_matches():
    assert CE._ranking_select_columns(["foo", "bar"]) == "*"


def test_projection_can_be_disabled(monkeypatch):
    monkeypatch.setenv("SUMMARY_ENRICH_RANKING_PROJECT_COLUMNS", "0")
    assert CE._ranking_select_columns(["symbol", "datetime"]) == "*"


def test_cache_returns_copy_so_caller_cannot_corrupt_it(monkeypatch):
    a = pd.DataFrame({"symbol": ["1301"], "rank": [1]})
    _stub(monkeypatch, [a])

    first = CE._read_ranking_df_from_db()
    first.loc[0, "rank"] = 999
    second = CE._read_ranking_df_from_db()

    assert second.loc[0, "rank"] == 1, "呼び出し側の変更がキャッシュを壊している"
