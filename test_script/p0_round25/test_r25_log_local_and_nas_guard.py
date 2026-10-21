# -*- coding: utf-8 -*-
"""2026-09-05: ログをローカルへ逃がし、NAS 処理に締め切りを付けた回帰テスト。

NAS がハングすると logging がブロックして全プロセスが止まり、
同時に調査手段 (ログ) も失う。実 NAS には触らず tmp_path で検証する。
"""
from __future__ import annotations

import importlib
import threading
import time

import pytest


# ============================================================
# 1. nas_guard — NAS 処理を時間で見切る共通部品
# ============================================================

def test_guard_returns_result_on_success():
    from utils.nas_guard import run_with_deadline

    res = run_with_deadline(lambda: 42, timeout_sec=5)
    assert res["ok"] is True
    assert res["result"] == 42
    assert res["timed_out"] is False


def test_guard_gives_up_and_returns():
    """終わらない処理でも呼び出し側は返る。これが本題。"""
    from utils.nas_guard import run_with_deadline

    release = threading.Event()
    t0 = time.perf_counter()
    res = run_with_deadline(lambda: release.wait(30), timeout_sec=0.5)
    elapsed = time.perf_counter() - t0

    assert res["timed_out"] is True
    assert res["ok"] is False
    assert elapsed < 5, f"見切りに {elapsed:.1f}s (呼び出し側が固まっている)"
    release.set()


def test_guard_swallows_exception():
    from utils.nas_guard import run_with_deadline

    def _boom():
        raise OSError("NAS 落ちた")

    res = run_with_deadline(_boom, timeout_sec=5)
    assert res["ok"] is False
    assert "NAS 落ちた" in res["error"]


def test_guard_slot_blocks_pileup():
    """詰まっている間は重ねない (SMB セッションを食い潰さないため)。"""
    from utils.nas_guard import new_slot, run_with_deadline

    slot = new_slot()
    release = threading.Event()

    first = run_with_deadline(lambda: release.wait(30), timeout_sec=0.3, slot=slot)
    assert first["timed_out"] is True

    second = run_with_deadline(lambda: 1, timeout_sec=0.3, slot=slot)
    assert second["skipped"] is True
    release.set()


def test_guard_slot_is_released_after_success():
    from utils.nas_guard import new_slot, run_with_deadline

    slot = new_slot()
    assert run_with_deadline(lambda: 1, timeout_sec=5, slot=slot)["ok"] is True
    assert run_with_deadline(lambda: 2, timeout_sec=5, slot=slot)["result"] == 2


# ============================================================
# 2. ログの書き先がローカルであること
# ============================================================

def test_log_dir_defaults_to_local(monkeypatch, tmp_path):
    """既定が NAS に戻ると、NAS のハングで logging ごと止まる。"""
    monkeypatch.delenv("KABU_CONSOLE_LOG_DIR", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    from data_collectors import config

    cfg = importlib.reload(config)
    assert str(tmp_path) in str(cfg.LOG_DIR)
    assert not str(cfg.LOG_DIR).startswith("X:")
    assert not str(cfg.LOG_DIR).startswith("\\\\")


def test_log_dir_env_override_still_works(monkeypatch, tmp_path):
    monkeypatch.setenv("KABU_CONSOLE_LOG_DIR", str(tmp_path / "somewhere"))
    from data_collectors import config

    cfg = importlib.reload(config)
    assert cfg.LOG_DIR == tmp_path / "somewhere"


def test_console_tee_default_is_not_evaluated_at_import():
    """既定値を import 時に決めると、import しただけで NAS に触る。"""
    import inspect

    from core.logging import console_tee

    sig = inspect.signature(console_tee.setup_console_tee)
    assert sig.parameters["log_dir"].default is None, (
        "log_dir の既定が import 時に評価されている"
    )


def test_console_tee_default_dir_is_local(monkeypatch, tmp_path):
    monkeypatch.setenv("KABU_CONSOLE_LOG_DIR", str(tmp_path / "c"))
    import data_collectors.config as cfg
    importlib.reload(cfg)

    from core.logging import console_tee

    assert console_tee._default_console_log_dir() == tmp_path / "c"


# ============================================================
# 3. ログの NAS ミラー
# ============================================================

@pytest.fixture
def mirror(tmp_path, monkeypatch):
    monkeypatch.setenv("KABU_LOG_NAS_DIR", str(tmp_path / "nas"))
    monkeypatch.delenv("KABU_LOG_MIRROR_TO_NAS", raising=False)
    from data_collectors import log_mirror

    m = importlib.reload(log_mirror)
    return m


def test_mirror_sends_only_the_new_part(mirror, tmp_path):
    """1 日 60MB を毎回まるごと送らない。差分だけ追記する。"""
    local = tmp_path / "local"
    local.mkdir()
    p = local / "app_20260905_1.log"
    p.write_text("1行目\n2行目\n", encoding="utf-8")
    mirror.register(p)

    first = mirror.mirror_once(reason="t1")
    assert first["ok"] is True
    n1 = first["result"]["bytes"]
    assert n1 > 0

    with open(p, "a", encoding="utf-8") as f:
        f.write("3行目\n")

    second = mirror.mirror_once(reason="t2")
    assert second["ok"] is True
    n2 = second["result"]["bytes"]
    assert 0 < n2 < n1, "差分ではなく全体を送っている"

    dst = mirror.nas_log_dir() / p.name
    assert dst.read_text(encoding="utf-8") == p.read_text(encoding="utf-8")


def test_mirror_resends_when_file_shrinks(mirror, tmp_path):
    """ローテーションで縮んだら送り直す (追記位置がずれたままにしない)。"""
    local = tmp_path / "local"
    local.mkdir()
    p = local / "app.log"
    p.write_text("aaaa\nbbbb\n", encoding="utf-8")
    mirror.register(p)
    mirror.mirror_once()

    p.write_text("x\n", encoding="utf-8")   # 縮む
    mirror.mirror_once()

    dst = mirror.nas_log_dir() / p.name
    assert dst.read_text(encoding="utf-8") == "x\n"


def test_mirror_can_be_disabled(mirror, monkeypatch, tmp_path):
    monkeypatch.setenv("KABU_LOG_MIRROR_TO_NAS", "0")
    local = tmp_path / "local"
    local.mkdir()
    p = local / "a.log"
    p.write_text("x\n", encoding="utf-8")
    mirror.register(p)
    res = mirror.mirror_once()
    assert res.get("skipped") is True
    assert not (mirror.nas_log_dir() / p.name).exists()


def test_mirror_does_not_raise_when_nas_hangs(mirror, tmp_path, monkeypatch):
    """ミラーが固まっても呼び出し側は返る。ログ出力は止めない。"""
    local = tmp_path / "local"
    local.mkdir()
    p = local / "a.log"
    p.write_text("x\n", encoding="utf-8")
    mirror.register(p)

    release = threading.Event()
    monkeypatch.setattr(mirror, "_mirror_all", lambda: release.wait(30))
    monkeypatch.setenv("KABU_LOG_MIRROR_TIMEOUT_SEC", "5")

    t0 = time.perf_counter()
    res = mirror.mirror_once(reason="hang")
    elapsed = time.perf_counter() - t0

    assert res["timed_out"] is True
    assert elapsed < 15
    release.set()
