# -*- coding: utf-8 -*-
"""2026-09-07: ranking DB をローカルへ移した回帰テスト。

NAS 上の ranking DB (427MB + WAL 129MB) は `ranking_snapshot_1min` の
読み取りが 2 秒の予算に間に合わず err=interrupted になり、候補が作れず
(`no_ranking_df`)、**毎日エントリーが 1 件も出ていなかった**。

このテストが守るのは「writer と読み手が同じ場所を返すこと」。
1 箇所でも NAS 固定に戻ると、その読み手だけが複製 (最大で複製間隔ぶん
古い) を黙って読む。ranking DB は銘柄選定を駆動しているので、
古いデータを読む状態は「読めない」より悪い。

実 NAS には触らない。すべて tmp_path 上で行う。
"""
from __future__ import annotations

import datetime as dt
import importlib
import sqlite3
import threading
import time

import pytest


@pytest.fixture
def loc(tmp_path, monkeypatch):
    monkeypatch.setenv("RANKING_DB_LOCAL_DIR", str(tmp_path / "local"))
    monkeypatch.setenv("RANKING_DB_NAS_DIR", str(tmp_path / "nas"))
    for k in ("RANKING_DB_PATH", "ATS_RANKING_DB_PATH", "KABU_RANKING_DB_PATH",
              "RANKING_DB_LOCAL_STAGING", "RANKING_DB_DIR", "KABU_RANKING_DB_DIR"):
        monkeypatch.delenv(k, raising=False)
    from trading.ranking import ranking_db_location

    return importlib.reload(ranking_db_location)


# ============================================================
# 1. 置き場所
# ============================================================

def test_default_is_local(loc):
    assert loc.local_staging_enabled() is True
    p = loc.ranking_db_path()
    assert "local" in str(p)
    assert p.name == f"ranking{dt.datetime.now():%Y%m%d}.db"


def test_can_be_turned_off(loc, monkeypatch):
    """従来どおり NAS へ直接書く逃げ道を残す。"""
    monkeypatch.setenv("RANKING_DB_LOCAL_STAGING", "0")
    assert loc.local_staging_enabled() is False
    assert "nas" in str(loc.ranking_db_path())


def test_explicit_env_path_wins(loc, monkeypatch, tmp_path):
    explicit = tmp_path / "explicit.db"
    monkeypatch.setenv("RANKING_DB_PATH", str(explicit))
    assert loc.ranking_db_path() == explicit


def test_candidates_put_local_first(loc):
    """取りこぼしがあっても NAS へ落ちて動き続けるが、順序は必ずローカル優先。"""
    c = loc.ranking_db_candidates()
    assert len(c) == 2
    assert "local" in c[0]
    assert "nas" in c[1]


# ============================================================
# 2. writer と読み手が同じ場所を返すこと (本命)
# ============================================================

def test_writer_and_ats_resolver_agree(loc):
    """ここがずれると、読み手だけが古い複製を読む。"""
    import ats.ats_ranking.constants as ats_const
    import ats.ats_ranking.db_path as ats_db_path
    import database.paths.ranking_paths as rp

    importlib.reload(ats_const)
    importlib.reload(ats_db_path)
    importlib.reload(rp)

    expected = str(loc.ranking_db_path())
    assert rp.get_ranking_db_path() == expected, "writer が別の場所を見ている"
    assert ats_db_path.get_today_ranking_db_path() == expected, "ATS リゾルバがずれている"
    assert str(ats_const.RANKING_DB_ROOT) == str(loc.ranking_db_dir())


def test_writer_path_is_not_import_time_frozen(loc, monkeypatch, tmp_path):
    """既定値を引数のデフォルトに固定すると env 切り替えが効かなくなる。"""
    import database.paths.ranking_paths as rp

    importlib.reload(rp)
    moved = tmp_path / "moved"
    monkeypatch.setenv("RANKING_DB_LOCAL_DIR", str(moved))
    importlib.reload(loc)
    assert str(moved) in rp.get_ranking_db_path()


@pytest.mark.parametrize(
    "module_name,attr",
    [
        ("trading.push.subscription_manager.ranking_source_paths", "RANKING_DB_BASE_DIRS"),
    ],
)
def test_candidate_lists_start_with_local(loc, module_name, attr, monkeypatch):
    import importlib as il

    mod = il.reload(il.import_module(module_name))
    dirs = getattr(mod, attr)
    assert dirs, f"{attr} が空"
    assert "local" in str(dirs[0]), f"{attr} の先頭がローカルでない: {dirs[0]}"


def test_readers_reference_the_resolver():
    """移行から漏れた読み手が無いこと。

    ここを増やすときは「その読み手が ranking_db_location を経由するか」
    を必ず確認する。
    """
    import inspect

    from trading.entry import volume_direction_guard
    from trading.filters import volatility_filter
    from trading.handlers import entry_precheck_ranking
    from trading.push.push_stream import rotation_stability
    from trading.push.subscription_manager import ranking_selector

    for mod in (volume_direction_guard, volatility_filter, entry_precheck_ranking,
                rotation_stability, ranking_selector):
        src = inspect.getsource(mod)
        assert "ranking_db_location" in src, f"{mod.__name__} が resolver を経由していない"


# ============================================================
# 3. NAS への複製
# ============================================================

def _make_db(path, rows=300):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE ranking_snapshot_1min(a INTEGER, b TEXT)")
    conn.executemany("INSERT INTO ranking_snapshot_1min VALUES (?,?)",
                     [(i, "x" * 40) for i in range(rows)])
    conn.commit()
    return conn


def test_sync_copies_rows_and_is_consistent(loc):
    conn = _make_db(loc.ranking_db_path(), rows=456)
    try:
        res = loc.sync_to_nas(reason="test")
        assert res["ok"] is True, res["error"]
        dst = sqlite3.connect(str(loc.nas_ranking_db_path()))
        try:
            assert dst.execute("SELECT COUNT(*) FROM ranking_snapshot_1min").fetchone()[0] == 456
            assert dst.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            dst.close()
    finally:
        conn.close()


def test_failed_sync_keeps_previous_copy(loc, monkeypatch):
    """複製に失敗しても、前回の正常な複製を壊さない。"""
    conn = _make_db(loc.ranking_db_path(), rows=222)
    try:
        assert loc.sync_to_nas(reason="first")["ok"] is True
        dst = loc.nas_ranking_db_path()
        good = dst.stat().st_size

        real = sqlite3.connect

        def _fail_on_tmp(path, *a, **k):
            if ".tmp" in str(path):
                raise OSError("複製先に書けない")
            return real(path, *a, **k)

        monkeypatch.setattr(sqlite3, "connect", _fail_on_tmp)
        assert loc.sync_to_nas(reason="second")["ok"] is False
    finally:
        conn.close()

    assert dst.stat().st_size == good, "失敗した複製が既存の複製を壊した"
    check = sqlite3.connect(str(dst))
    try:
        assert check.execute("SELECT COUNT(*) FROM ranking_snapshot_1min").fetchone()[0] == 222
    finally:
        check.close()


def test_sync_gives_up_when_nas_hangs(loc, monkeypatch):
    """NAS がハングしても呼び出し側は返る。"""
    _make_db(loc.ranking_db_path(), rows=10).close()
    release = threading.Event()
    monkeypatch.setattr(loc, "_do_backup", lambda *a, **k: release.wait(30))

    t0 = time.perf_counter()
    res = loc.sync_to_nas(reason="hang", timeout_sec=1.0)
    elapsed = time.perf_counter() - t0

    assert res["timed_out"] is True
    assert elapsed < 10, f"見切りに {elapsed:.1f}s (呼び出し側が固まっている)"
    release.set()


def test_sync_leaves_no_tmp_behind(loc):
    conn = _make_db(loc.ranking_db_path(), rows=50)
    try:
        assert loc.sync_to_nas(reason="tmp")["ok"] is True
    finally:
        conn.close()
    leftovers = [p.name for p in loc.nas_ranking_db_path().parent.iterdir() if ".tmp" in p.name]
    assert leftovers == []


def test_writer_syncs_on_stop():
    """大引け後の停止で今日ぶんが NAS へ渡ること。"""
    import inspect

    from trading.ranking.ranking_db_writer import RankingDBWriter

    assert "_sync_to_nas_safe" in inspect.getsource(RankingDBWriter.stop)
    assert "_start_bg_nas_sync" in inspect.getsource(RankingDBWriter)


# ============================================================
# 4. 実在しない関数を import して握り潰していないこと
#    (2026-09-08: entry_from_ranking が ats.ats_ranking.db_path から
#     resolve_ranking_db_path を import していた。その名前は存在せず、
#     ImportError が debug で握り潰されてハードコードされた NAS の UNC
#     パスへ落ちていた。そのため RANKING_DB_ROOT をローカルへ向けても
#     この読み手だけは NAS を読み続け、db read empty -> no_ranking_df に
#     なっていた。エントリーが出ない直接の原因。)
# ============================================================

def test_ats_db_path_does_not_export_resolve_ranking_db_path():
    """存在しない名前を当てにしない。実在する API を確認する。"""
    import ats.ats_ranking.db_path as d

    assert not hasattr(d, "resolve_ranking_db_path"), (
        "resolve_ranking_db_path が生えたなら、それを使う側の意図を確認すること"
    )
    assert hasattr(d, "get_usable_ranking_db_path")
    assert hasattr(d, "get_today_ranking_db_path")


@pytest.mark.parametrize(
    "module_name",
    [
        "trading.ranking.entry_from_ranking",
        "trading.entry.volume_direction_guard",
    ],
)
def test_no_import_of_missing_resolver(module_name):
    """壊れた import が残っていないこと。"""
    import importlib
    import inspect

    src = inspect.getsource(importlib.import_module(module_name))
    assert "import resolve_ranking_db_path" not in src, (
        f"{module_name} が存在しない resolve_ranking_db_path を import している"
    )


def test_entry_from_ranking_resolves_local_first(loc):
    """本命の読み手がローカルを返すこと。

    ここが NAS を返すと `db read empty` -> `no_ranking_df` -> pending 0 で
    エントリーが 1 件も出なくなる (2026-09-07/08 に実際に起きた)。
    """
    import importlib

    from trading.ranking import entry_from_ranking

    importlib.reload(entry_from_ranking)

    # ローカルに実体が無いと candidates が NAS へ落ちるので、作ってから確認する
    _make_db(loc.ranking_db_path(), rows=5).close()
    resolved = entry_from_ranking._rsrc_resolve_db_path()
    assert resolved == str(loc.ranking_db_path()), f"NAS を見ている: {resolved}"
