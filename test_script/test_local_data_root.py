# 2026-10-24: 場中は NAS を使わない (nas_root のローカルのデータ root と ops/local_run/local_data_sync.py)
import importlib
import os
import sqlite3
import sys
from pathlib import Path

import pytest

import nas_root as N

sys.path.insert(0, str(Path(N.__file__).resolve().parent / "ops" / "local_run"))
LDS = importlib.import_module("local_data_sync")


@pytest.fixture
def roots(tmp_path, monkeypatch):
    local = tmp_path / "local"
    nas = tmp_path / "nas"
    local.mkdir()
    nas.mkdir()
    monkeypatch.setenv("AUTOSTOCK_LOCAL_DATA_ROOT", str(local))
    monkeypatch.setenv("AUTOSTOCK_NAS_ROOT", str(nas))
    monkeypatch.delenv("AUTOSTOCK_LOCAL_DATA", raising=False)
    N.reset_cache_for_test()
    yield local, nas
    N.reset_cache_for_test()


def _ready(local):
    (local / N.READY_MARKER).write_text("x", encoding="utf-8")
    N.reset_cache_for_test()


def test_without_marker_uses_nas(roots):
    local, nas = roots
    assert N.nas_root() == str(nas)
    assert N.to_real_nas(local / "a") == str(local / "a")      # ローカル無効なら読み替えない


def test_with_marker_uses_local(roots):
    local, nas = roots
    _ready(local)
    assert N.nas_root() == str(local)
    assert N.nas_path(r"raw_data\kabu_station\summary") == str(local) + r"\raw_data\kabu_station\summary"
    assert N.real_nas_root() == str(nas)
    assert N.to_real_nas(str(local) + r"\raw_data\x.db") == str(nas) + r"\raw_data\x.db"
    assert N.to_real_nas(r"C:\other\x.db") == r"C:\other\x.db"


def test_switch_off(roots, monkeypatch):
    local, nas = roots
    _ready(local)
    monkeypatch.setenv("AUTOSTOCK_LOCAL_DATA", "0")
    N.reset_cache_for_test()
    assert N.nas_root() == str(nas)


def _make_db(p: Path, rows, *, keep_open=False):
    p.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(p))
    c.execute("pragma journal_mode=wal")
    c.execute("pragma wal_autocheckpoint=0")
    c.execute("create table if not exists t(v integer)")
    c.executemany("insert into t values (?)", [(r,) for r in rows])
    c.commit()
    if keep_open:
        return c
    c.close()
    return None


def _count(p: Path) -> int:
    c = sqlite3.connect(str(p))
    try:
        return c.execute("select count(*) from t").fetchone()[0]
    finally:
        c.close()


def test_to_nas_copies_live_wal_db_and_drops_stale_wal(roots):
    local, nas = roots
    _ready(local)
    src = local / "raw_data" / "kabu_station" / "summary" / "summary20261026.db"
    writer = _make_db(src, range(5), keep_open=True)          # 書き手がいて -wal に未反映の行がある
    assert Path(str(src) + "-wal").stat().st_size > 0

    dst = nas / src.relative_to(local)
    dst.parent.mkdir(parents=True)
    dst.write_bytes(b"old")
    Path(str(dst) + "-wal").write_bytes(b"stale wal")          # 古い -wal が残っている NAS
    os.utime(dst, (1_000_000, 1_000_000))                       # NAS 側は前回の写し (古い)

    assert LDS.to_nas(local, days=1, per_file_timeout=60, dry=False) == 0
    writer.close()
    assert not Path(str(dst) + "-wal").exists()
    assert _count(dst) == 5


def test_to_nas_skips_when_nas_is_newer(roots):
    local, nas = roots
    _ready(local)
    src = local / "Basic" / "a.txt"
    src.parent.mkdir()
    src.write_text("local")
    dst = nas / "Basic" / "a.txt"
    dst.parent.mkdir()
    dst.write_text("nas")
    os.utime(src, (1_000_000, 1_000_000))
    LDS.to_nas(local, days=0, per_file_timeout=60, dry=False)
    assert dst.read_text() == "nas"


def test_from_nas_copies_db_with_wal(roots):
    local, nas = roots
    src = nas / "raw_data" / "trade_guard" / "trade_guard20261023.db"
    w = _make_db(src, range(7), keep_open=True)
    # NAS 側は書き手のいない状態で写す前提。-wal が残っている形を作るため接続を残したまま写す
    assert LDS.from_nas(local, [(r"raw_data\trade_guard", 0)], per_file_timeout=60, dry=False) == 0
    w.close()
    assert _count(local / src.relative_to(nas)) == 7


def test_setup_makes_junctions(roots, tmp_path, monkeypatch):
    local, nas = roots
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "la"))
    LDS.setup(local)
    for rel, target in LDS._junctions().items():
        assert LDS._is_junction(local / rel)
        (target / "probe.txt").write_text("1")
        assert (local / rel / "probe.txt").read_text() == "1"
    LDS.setup(local)                                           # 2 回目はそのまま
