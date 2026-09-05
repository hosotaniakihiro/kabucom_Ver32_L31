# -*- coding: utf-8 -*-
"""2026-09-05: push DB をローカルへ書き、NAS へ複製する仕組みの回帰テスト。

実 NAS・実 PUSH・実 API は使わない。すべて tmp_path 上で行う。
"""
from __future__ import annotations

import datetime as dt
import importlib
import os
import sqlite3
import sys

import pytest


@pytest.fixture
def loc(tmp_path, monkeypatch):
    """push_db_location を tmp_path 上で使う。"""
    local_dir = tmp_path / "local"
    nas_dir = tmp_path / "nas"
    monkeypatch.setenv("PUSH_DB_LOCAL_DIR", str(local_dir))
    monkeypatch.setenv("PUSH_DB_NAS_DIR", str(nas_dir))
    monkeypatch.delenv("PUSH_DB_PATH", raising=False)
    monkeypatch.delenv("AUTOSTOCK_PUSH_DB_PATH", raising=False)
    monkeypatch.delenv("PUSH_DB_LOCAL_STAGING", raising=False)
    from trading.push import push_db_location

    return importlib.reload(push_db_location)


# ============================================================
# 1. 置き場所の決まり方
# ============================================================

def test_default_is_local(loc):
    """既定はローカル。ここが NAS に戻ると 2026-09-04 の詰まりが再発する。"""
    assert loc.local_staging_enabled() is True
    p = loc.push_db_path()
    assert "local" in str(p)
    assert p.name == f"push{dt.datetime.now():%Y%m%d}.db"


def test_can_be_turned_off(loc, monkeypatch):
    """従来どおり NAS へ直接書く逃げ道を残す。"""
    monkeypatch.setenv("PUSH_DB_LOCAL_STAGING", "0")
    assert loc.local_staging_enabled() is False
    assert "nas" in str(loc.push_db_path())


def test_explicit_path_wins(loc, monkeypatch, tmp_path):
    """PUSH_DB_PATH は従来どおり最優先。"""
    explicit = tmp_path / "explicit.db"
    monkeypatch.setenv("PUSH_DB_PATH", str(explicit))
    assert loc.push_db_path() == explicit


def test_nas_path_is_independent_of_staging(loc, monkeypatch):
    """複製先は staging の on/off で変わらない。"""
    before = loc.nas_push_db_path()
    monkeypatch.setenv("PUSH_DB_LOCAL_STAGING", "0")
    assert loc.nas_push_db_path() == before


# ============================================================
# 2. 書き手と読み手が同じ場所を見ること
#    (ここがずれると、場中ずっと前日の NAS 複製を読む)
# ============================================================

def test_writer_and_readers_agree(loc, monkeypatch):
    from scheduler_jobs.summary import fallback_loader, memory_latest_1m

    expected = loc.push_db_path()

    monkeypatch.delenv("PUSH_DB_DIR", raising=False)
    monkeypatch.delenv("RAW_PUSH_DIR", raising=False)

    assert memory_latest_1m._push_db_path() == expected
    assert str(fallback_loader._push_db_path()) == str(expected)


def test_writer_resolves_to_same_path(loc):
    """writer の _resolve_db_path も同じ場所を返す。"""
    from trading.push.push_db_writer import StreamDBWriter

    w = StreamDBWriter.__new__(StreamDBWriter)
    assert w._resolve_db_path() == loc.push_db_path()


# ============================================================
# 3. NAS への複製
# ============================================================

def _make_db(path, rows=500):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t(a INTEGER, b TEXT)")
    conn.executemany("INSERT INTO t VALUES (?,?)", [(i, "x" * 50) for i in range(rows)])
    conn.commit()
    return conn


def test_sync_copies_all_rows(loc):
    conn = _make_db(loc.push_db_path(), rows=1234)
    try:
        res = loc.sync_to_nas(reason="test")
        assert res["ok"] is True, res["error"]
        dst = sqlite3.connect(str(loc.nas_push_db_path()))
        try:
            assert dst.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1234
            assert dst.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            dst.close()
    finally:
        conn.close()


def test_sync_works_while_source_is_open(loc):
    """稼働中の DB を複製できること。

    shutil.copy だと壊れるので backup() を使っている。ここが
    ファイルコピーに戻ると、大引け後の複製が壊れた DB を NAS へ置く。
    """
    conn = _make_db(loc.push_db_path(), rows=200)
    try:
        conn.execute("INSERT INTO t VALUES (9999, 'uncommitted')")  # 未 commit のまま
        res = loc.sync_to_nas(reason="live")
        assert res["ok"] is True, res["error"]
        dst = sqlite3.connect(str(loc.nas_push_db_path()))
        try:
            assert dst.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            # 未 commit の行は複製に含まれない (これが一貫性のある複製)
            assert dst.execute("SELECT COUNT(*) FROM t WHERE a=9999").fetchone()[0] == 0
        finally:
            dst.close()
    finally:
        conn.close()


def test_sync_missing_source_does_not_raise(loc):
    """元が無くても例外にしない。取り込みを止めないため。"""
    res = loc.sync_to_nas(reason="missing")
    assert res["ok"] is False
    assert "source" in res["error"]


def test_sync_is_noop_when_staging_disabled(loc, monkeypatch):
    monkeypatch.setenv("PUSH_DB_LOCAL_STAGING", "0")
    res = loc.sync_to_nas(reason="off")
    assert res["ok"] is True
    assert not loc.nas_push_db_path().exists()


def test_sync_failure_is_swallowed(loc, monkeypatch):
    """複製が失敗しても例外を投げない (ローカルの取り込みが最優先)。"""
    _make_db(loc.push_db_path(), rows=10).close()

    def _boom(*a, **k):
        raise OSError("NAS 落ちた")

    monkeypatch.setattr(sqlite3, "connect", _boom)
    res = loc.sync_to_nas(reason="broken")
    assert res["ok"] is False
    assert "NAS 落ちた" in res["error"]


# ============================================================
# 4. 定期複製の間隔
# ============================================================

def test_sync_interval_default_and_off(loc, monkeypatch):
    assert loc.nas_sync_interval_sec() == pytest.approx(30 * 60)
    monkeypatch.setenv("PUSH_DB_NAS_SYNC_INTERVAL_MIN", "0")
    assert loc.nas_sync_interval_sec() == 0
    monkeypatch.setenv("PUSH_DB_NAS_SYNC_INTERVAL_MIN", "5")
    assert loc.nas_sync_interval_sec() == pytest.approx(300)


def test_writer_syncs_on_stop_and_rotate():
    """stop と rotate から複製が呼ばれること。落とすと日次の複製が消える。"""
    import inspect

    from trading.push.push_db_writer import StreamDBWriter

    assert "_sync_to_nas_safe" in inspect.getsource(StreamDBWriter.stop)
    assert "_sync_to_nas_safe" in inspect.getsource(StreamDBWriter._rotate_if_needed_locked)


# ============================================================
# 5. NAS がハングしても取り込みを止めないこと
#    (2026-09-05 のユーザー報告: 最近 NAS がハングすることがある)
#    SMB のハングは sqlite3 の timeout では抜けられず OS の I/O で
#    ブロックするので、時間で見切れることを固定しておく。
# ============================================================

def test_sync_gives_up_when_nas_hangs(loc, monkeypatch):
    """複製が終わらなくても呼び出し側は返ってくること。"""
    import threading
    import time as _time

    _make_db(loc.push_db_path(), rows=10).close()

    started = threading.Event()
    release = threading.Event()

    def _hang(*a, **k):
        started.set()
        release.wait(30)          # NAS が返ってこない状態を作る
        # 解放されたら静かに終わる (放置スレッドが例外を出すと
        # pytest が warning を出すだけで、検証したいことではない)

    monkeypatch.setattr(loc, "_do_backup", lambda *a, **k: _hang())

    t0 = _time.perf_counter()
    res = loc.sync_to_nas(reason="hang", timeout_sec=1.0)
    elapsed = _time.perf_counter() - t0

    assert started.wait(5), "複製ワーカーが起動していない"
    assert res["timed_out"] is True
    assert res["ok"] is False
    assert elapsed < 10, f"見切りに {elapsed:.1f}s かかった (呼び出し側が固まっている)"
    release.set()


def test_second_sync_is_skipped_while_one_is_stuck(loc, monkeypatch):
    """詰まっている間に複製を重ねない (SMB セッションを食い潰さないため)。"""
    import threading

    _make_db(loc.push_db_path(), rows=10).close()

    release = threading.Event()
    monkeypatch.setattr(loc, "_do_backup", lambda *a, **k: release.wait(30))

    first = loc.sync_to_nas(reason="stuck", timeout_sec=0.5)
    assert first["timed_out"] is True

    second = loc.sync_to_nas(reason="next", timeout_sec=0.5)
    assert second["skipped"] is True, "詰まっているのに複製を重ねている"
    release.set()


def test_rotate_does_not_wait_for_nas():
    """ローテーションは writer のロックを握ったまま呼ばれる。

    ここで NAS を待つと、日付が変わる瞬間に NAS がハングしたとき
    PUSH の取り込みごと止まる。必ず wait=False で投げること。
    """
    import inspect

    from trading.push.push_db_writer import StreamDBWriter

    src = inspect.getsource(StreamDBWriter._rotate_if_needed_locked)
    assert "wait=False" in src, "ローテーションが NAS の複製を待っている"


def test_stop_sync_has_a_deadline():
    """停止時の複製は待つが、無限には待たない。"""
    import inspect

    from trading.push import push_db_writer as m

    src = inspect.getsource(m.StreamDBWriter.stop)
    assert "timeout_sec=" in src, "停止時の複製に上限が無い"
    assert "slot_wait_sec=" in src, "定期複製と重なったとき最後のぶんを取りこぼす"
    assert m._push_nas_sync_stop_timeout_sec() > 0
