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
    tok = PI._inflight_try_acquire("push", 1, now)
    assert isinstance(tok, int)
    assert PI._inflight_try_acquire("push", 1, now) is None, "同じジョブの二重取得は拒否されること"


def test_different_source_or_interval_is_independent():
    now = time.perf_counter()
    assert PI._inflight_try_acquire("push", 1, now) is not None
    assert PI._inflight_try_acquire("push", 3, now) is not None
    assert PI._inflight_try_acquire("ranking", 1, now) is not None


def test_release_allows_next_tick():
    now = time.perf_counter()
    tok = PI._inflight_try_acquire("push", 1, now)
    PI._inflight_release("push", 1, tok)
    assert PI._inflight_try_acquire("push", 1, now) is not None, "解放後は再取得できること"


def test_release_without_token_does_nothing():
    """token を持たない呼び出し元は解放できない。

    bg ジョブなど acquire していない経路が
    親 tick の登録を横から消してしまうのを防ぐ。
    """
    now = time.perf_counter()
    PI._inflight_try_acquire("push", 1, now)
    PI._inflight_release("push", 1)
    assert ("push", 1) in PI._INFLIGHT, "token なしで解放されてはいけない"


def test_stale_worker_finally_does_not_release_new_generation():
    """古いワーカーの finally が新しい世代の登録を消さないこと。"""
    now = time.perf_counter()
    old_tok = PI._inflight_try_acquire("push", 1, now)
    PI._inflight_release("push", 1, old_tok)

    new_tok = PI._inflight_try_acquire("push", 1, now)
    assert new_tok != old_tok

    # 遅れて到着した古いワーカーの finally
    PI._inflight_release("push", 1, old_tok)
    assert ("push", 1) in PI._INFLIGHT, "古い token で新しい世代が消された"
    assert PI._INFLIGHT[("push", 1)].token == new_tok


def test_job_releases_even_when_body_raises(monkeypatch):
    """例外時も解放されること。token を渡した場合のみ解放する。"""
    now = time.perf_counter()
    tok = PI._inflight_try_acquire("push", 1, now)

    def _boom(**kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(PI, "_job_one_source_core", _boom)
    with pytest.raises(RuntimeError):
        PI._job_one_source(
            source="push", interval=1, now=None, display=False, run_entry=False,
            inflight_token=tok,
        )

    assert ("push", 1) not in PI._INFLIGHT, "例外時に解放されていない"


def test_inflight_snapshot_reports_age():
    PI._inflight_try_acquire("push", 1, time.perf_counter())
    snap = PI.inflight_snapshot()
    assert "push:1" in snap
    assert snap["push:1"] >= 0.0


# ============================================================
# inflight 実体診断と stale 回収 (2026-08-25)
# ------------------------------------------------------------
# push:1 が 10:02-11:58 の 230 tick 連続で skip され、
# Summary が 09:54 で止まった。
# 「worker が生きている」「future だけ残っている」「登録だけ残っている」
# を区別できないと、安全に回収してよいか判断できない。
# ============================================================

class _FakeFuture:
    def __init__(self, done=False, running=False, cancelled=False):
        self._done, self._running, self._cancelled = done, running, cancelled

    def done(self):
        return self._done

    def running(self):
        return self._running

    def cancelled(self):
        return self._cancelled


def test_diagnose_reports_all_required_fields():
    now = time.perf_counter()
    tok = PI._inflight_try_acquire("push", 1, now, owner="tick:test")
    PI._inflight_attach_future("push", 1, tok, _FakeFuture(running=True))
    PI._inflight_mark_thread("push", 1, tok)

    info = PI._inflight_diagnose("push", 1, now + 100.0)
    for field in ("key", "generation", "owner", "started_at", "age",
                  "done", "running", "cancelled", "thread", "thread_alive"):
        assert field in info, "診断に %s が無い" % field
    assert info["key"] == "push:1"
    assert info["generation"] == tok
    assert info["owner"] == "tick:test"
    assert info["age"] >= 100.0
    assert info["running"] is True
    assert info["thread_alive"] is True


def test_running_worker_is_never_reclaimed():
    """走っているワーカーは何秒経っても回収しない (二重起動の禁止)。"""
    now = time.perf_counter()
    tok = PI._inflight_try_acquire("push", 1, now)
    PI._inflight_attach_future("push", 1, tok, _FakeFuture(running=True))
    PI._inflight_mark_thread("push", 1, tok)

    info = PI._inflight_diagnose("push", 1, now + 7200.0)
    assert PI._inflight_try_reclaim("push", 1, info) is None
    assert ("push", 1) in PI._INFLIGHT, "走っているのに回収された"


def test_cancelled_future_is_reclaimed():
    now = time.perf_counter()
    tok = PI._inflight_try_acquire("push", 1, now)
    PI._inflight_attach_future("push", 1, tok, _FakeFuture(cancelled=True))

    info = PI._inflight_diagnose("push", 1, now + 5.0)
    assert PI._inflight_try_reclaim("push", 1, info) == "future_cancelled"
    assert ("push", 1) not in PI._INFLIGHT
    assert PI._inflight_try_acquire("push", 1, now) is not None, "回収後に再投入できること"


def test_queued_but_never_started_is_reclaimed_after_stale_sec():
    """executor が差し替えられ queue ごと捨てられた future を回収する。"""
    now = time.perf_counter()
    tok = PI._inflight_try_acquire("push", 1, now)
    PI._inflight_attach_future("push", 1, tok, _FakeFuture(done=False, running=False))

    young = PI._inflight_diagnose("push", 1, now + 1.0)
    assert PI._inflight_try_reclaim("push", 1, young) is None, "若いうちは回収しない"

    old = PI._inflight_diagnose("push", 1, now + PI._inflight_stale_reclaim_sec() + 1.0)
    assert PI._inflight_try_reclaim("push", 1, old) == "queued_but_executor_gone"
    assert ("push", 1) not in PI._INFLIGHT


def test_never_submitted_is_reclaimed_after_stale_sec():
    now = time.perf_counter()
    PI._inflight_try_acquire("push", 1, now)
    info = PI._inflight_diagnose("push", 1, now + PI._inflight_stale_reclaim_sec() + 1.0)
    assert PI._inflight_try_reclaim("push", 1, info) == "never_submitted"
    assert ("push", 1) not in PI._INFLIGHT


def test_reclaim_ignores_other_generation():
    """診断した世代と今の世代が違えば触らない。"""
    now = time.perf_counter()
    tok = PI._inflight_try_acquire("push", 1, now)
    PI._inflight_attach_future("push", 1, tok, _FakeFuture(cancelled=True))
    info = PI._inflight_diagnose("push", 1, now + 5.0)

    # 別世代へ入れ替わる
    PI._inflight_release("push", 1, tok)
    new_tok = PI._inflight_try_acquire("push", 1, now)

    assert PI._inflight_try_reclaim("push", 1, info) is None
    assert PI._INFLIGHT[("push", 1)].token == new_tok, "別世代の登録が消された"


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
