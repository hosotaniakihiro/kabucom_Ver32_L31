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


# ============================================================
# 6. 複製が同時に 2 本走っても NAS の複製を壊さないこと
#    (2026-09-07 実測: main_database と push_receiver が両方
#     定期複製スレッドを立てており、同じ NAS ファイルへ
#     0.0MB と 78.5MB が 17 秒差で完了していた)
# ============================================================

def test_sync_writes_via_tmp_and_replaces(loc):
    """直接 dst へ書かない。出来上がってから置き換える。"""
    import inspect

    src = inspect.getsource(loc._do_backup)
    assert "os.replace" in src, "一時ファイルからの置き換えになっていない"
    assert ".tmp" in src


def test_sync_leaves_no_tmp_behind(loc):
    conn = _make_db(loc.push_db_path(), rows=50)
    try:
        assert loc.sync_to_nas(reason="tmp")["ok"] is True
    finally:
        conn.close()
    leftovers = [p.name for p in loc.nas_push_db_path().parent.iterdir() if ".tmp" in p.name]
    assert leftovers == [], f"一時ファイルが残っている: {leftovers}"


def test_existing_copy_survives_a_failed_sync(loc, monkeypatch):
    """複製に失敗しても、前回の正常な複製を壊さない。

    直接 dst へ backup していると、失敗や空の複製で NAS 側が壊れる。
    """
    conn = _make_db(loc.push_db_path(), rows=777)
    try:
        assert loc.sync_to_nas(reason="first")["ok"] is True
        dst = loc.nas_push_db_path()
        good = dst.stat().st_size

        real_connect = sqlite3.connect

        def _fail_on_tmp(path, *a, **k):
            if ".tmp" in str(path):
                raise OSError("複製先に書けない")
            return real_connect(path, *a, **k)

        monkeypatch.setattr(sqlite3, "connect", _fail_on_tmp)
        res = loc.sync_to_nas(reason="second")
        assert res["ok"] is False
    finally:
        conn.close()

    assert dst.stat().st_size == good, "失敗した複製が既存の複製を壊した"
    check = sqlite3.connect(str(dst))
    try:
        assert check.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 777
        assert check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        check.close()


def test_only_the_push_db_owner_starts_periodic_sync():
    """書き手でないプロセスは定期複製スレッドを立てない。"""
    import inspect

    from trading.push.push_db_writer import StreamDBWriter

    src = inspect.getsource(StreamDBWriter._start_bg_nas_sync)
    assert "_should_skip_push_db_writer_here" in src, (
        "複製側にオーナーガードが無い (2 プロセスが同じ NAS ファイルを書く)"
    )


# ============================================================
# 7. flush の 1 回あたり行数
#    (2026-09-08 実測: flush ループが 27 回/分 -> 2-4 回/分 へ落ちたとき
#     1 回 500 行では到着 約3,500 行/分 に追いつかず、キューが上限
#     200,000 に達して 152,066 行を捨てた。DB の最終 tick は 13:56 で停止)
# ============================================================

def test_buffer_size_default_is_large_enough():
    """4 回/分まで落ちても到着に追いつける行数であること。

    到着は実測で約 3,500 行/分。最悪 2 回/分でも 2 x buffer >= 3,500 が要る。
    """
    from trading.push import push_db_writer as m

    n = m._push_db_buffer_size()
    assert n >= 2000, f"1 回 {n} 行では flush ループが遅くなったときに追いつかない"
    assert 2 * n >= 3500, "最悪 2 回/分でも到着に追いつくこと"


def test_buffer_size_and_interval_are_env_tunable(monkeypatch):
    from trading.push import push_db_writer as m

    monkeypatch.setenv("PUSH_DB_BUFFER_SIZE", "1234")
    assert m._push_db_buffer_size() == 1234
    monkeypatch.setenv("PUSH_DB_FLUSH_INTERVAL_SEC", "0.5")
    assert m._push_db_flush_interval_sec() == pytest.approx(0.5)


def test_writer_uses_the_resolved_defaults():
    """既定値を引数のデフォルトに固定すると env 切り替えが効かなくなる。"""
    import inspect

    from trading.push.push_db_writer import StreamDBWriter

    sig = inspect.signature(StreamDBWriter.__init__)
    assert sig.parameters["buffer_size"].default == 0, (
        "buffer_size の既定が import 時に固定されている"
    )
    w = StreamDBWriter.__new__(StreamDBWriter)
    StreamDBWriter.__init__(w, enable_raw_save=False)
    from trading.push import push_db_writer as m

    assert w.buffer_size == m._push_db_buffer_size()
