"""Summary tick のワーカー滞留ガードと WAL チェックポイント格上げ。

背景 (2026-08-17 大引け後):
  親 tick は timeout=25s で待つのをやめて executor を reset するが、
  実行中の future はキャンセルできないのでワーカーは走り続ける。
  ジョブが tick 間隔 (60秒) を超えると毎分1本ずつ積み上がり、
  1本 7,000 秒超 / timeout 322 回中 324 tick まで悪化した。

  滞留したワーカーが summary DB を掴み続けるため WAL チェックポイントが
  PASSIVE で "skip busy" を繰り返し、WAL が 373MB まで伸びて、
  SMB 越しの読み取りが "file is not a database" を 142 件出す連鎖になった。
"""
from __future__ import annotations

import time

import pytest

from scheduler_jobs.summary import parallel_intervals as PI


@pytest.fixture(autouse=True)
def _clean():
    PI._INFLIGHT.clear()
    yield
    PI._INFLIGHT.clear()


def test_skip_when_busy_is_enabled_by_default(monkeypatch):
    monkeypatch.delenv("SUMMARY_PARALLEL_SKIP_WHEN_BUSY", raising=False)
    assert PI._skip_when_busy_enabled() is True


def test_second_acquire_for_same_job_is_refused():
    now = time.perf_counter()
    assert PI._inflight_try_acquire("push", 1, now) is True
    assert PI._inflight_try_acquire("push", 1, now) is False, "同じジョブが二重に走れてしまう"


def test_different_source_or_interval_is_independent():
    now = time.perf_counter()
    assert PI._inflight_try_acquire("push", 1, now) is True
    assert PI._inflight_try_acquire("push", 3, now) is True
    assert PI._inflight_try_acquire("ranking", 1, now) is True


def test_release_allows_next_tick():
    now = time.perf_counter()
    assert PI._inflight_try_acquire("push", 1, now) is True
    PI._inflight_release("push", 1)
    assert PI._inflight_try_acquire("push", 1, now) is True, "解放後に再投入できない"


def test_job_releases_even_when_body_raises(monkeypatch):
    """親が timeout しても、ワーカーは必ず解放して次 tick を通すこと。"""
    now = time.perf_counter()
    assert PI._inflight_try_acquire("push", 1, now) is True

    def _boom(**kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(PI, "_job_one_source_core", _boom)
    with pytest.raises(RuntimeError):
        PI._job_one_source(source="push", interval=1, now=None, display=False, run_entry=False)

    assert ("push", 1) not in PI._INFLIGHT, "例外時に解放されていない"


def test_inflight_snapshot_reports_age():
    PI._inflight_try_acquire("push", 1, time.perf_counter())
    snap = PI.inflight_snapshot()
    assert "push:1" in snap
    assert snap["push:1"] >= 0.0


# ============================================================
# WAL チェックポイントの格上げ判定
# ============================================================

def test_wal_escalation_requires_both_misses_and_size(monkeypatch):
    import main_database as MD

    monkeypatch.setenv("SUMMARY_WAL_CHECKPOINT_ESCALATE_AFTER_MISSES", "5")
    monkeypatch.setenv("SUMMARY_WAL_CHECKPOINT_ESCALATE_WAL_MB", "128")

    monkeypatch.setattr(MD, "_wal_checkpoint_wal_size_mb", lambda p=None: 200.0)
    assert MD._wal_checkpoint_should_escalate(4) is False, "空振り回数が足りないのに格上げした"
    assert MD._wal_checkpoint_should_escalate(5) is True

    monkeypatch.setattr(MD, "_wal_checkpoint_wal_size_mb", lambda p=None: 10.0)
    assert MD._wal_checkpoint_should_escalate(50) is False, "WAL が小さいのに格上げした"


def test_wal_escalation_can_be_disabled(monkeypatch):
    import main_database as MD

    monkeypatch.setenv("SUMMARY_WAL_CHECKPOINT_ESCALATE_ENABLED", "0")
    monkeypatch.setattr(MD, "_wal_checkpoint_wal_size_mb", lambda p=None: 999.0)
    assert MD._wal_checkpoint_should_escalate(100) is False


def test_default_checkpoint_mode_stays_passive(monkeypatch):
    import main_database as MD

    monkeypatch.delenv("SUMMARY_WAL_CHECKPOINT_MODE", raising=False)
    assert MD._wal_checkpoint_mode() == "PASSIVE", "通常時のモードを変えてはいけない"


# ============================================================
# WAL チェックポイントの対象 (2026-08-18)
# ============================================================

def test_checkpoint_targets_cover_ranking_and_push(monkeypatch):
    """summary だけでなく ranking / push も対象にすること。

    2026-08-18 後場、ranking DB の WAL が 282MB まで伸び (本体 444MB)、
    SMB 越しの読み取りが 1 回 4 秒 -> 13〜20 秒に悪化して
    1 tick が 333 秒になった。summary と同じ構造なので同じ扱いにする。
    """
    import main_database as MD

    monkeypatch.delenv("SUMMARY_WAL_CHECKPOINT_TARGETS", raising=False)
    names = [n for n, _ in MD._wal_checkpoint_targets()]
    assert "summary" in names
    assert "ranking" in names, "ranking DB が対象から漏れている"
    assert "push" in names


def test_checkpoint_targets_can_be_narrowed(monkeypatch):
    import main_database as MD

    monkeypatch.setenv("SUMMARY_WAL_CHECKPOINT_TARGETS", "summary")
    assert [n for n, _ in MD._wal_checkpoint_targets()] == ["summary"]


def test_escalation_is_evaluated_per_target(monkeypatch):
    """対象ごとに WAL サイズを見て格上げ判定すること。"""
    import main_database as MD
    from pathlib import Path as _P

    monkeypatch.setenv("SUMMARY_WAL_CHECKPOINT_ESCALATE_AFTER_MISSES", "5")
    monkeypatch.setenv("SUMMARY_WAL_CHECKPOINT_ESCALATE_WAL_MB", "128")

    big, small = _P("big.db"), _P("small.db")
    monkeypatch.setattr(MD, "_wal_checkpoint_wal_size_mb",
                        lambda p=None: 300.0 if p == big else 5.0)

    assert MD._wal_checkpoint_should_escalate(5, db_path=big) is True
    assert MD._wal_checkpoint_should_escalate(5, db_path=small) is False, (
        "WAL が小さい対象まで格上げしている"
    )
