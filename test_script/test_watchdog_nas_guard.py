# 2026-10-22: watchdog の NAS 自動復旧(つなぎ直し / 本体だけ古い足なら再起動)
import datetime as dt
import importlib.util
import os

import pytest

_P = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ops", "local_run", "watchdog.py")
_spec = importlib.util.spec_from_file_location("watchdog_under_test", _P)
WD = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(WD)

NOW = dt.datetime(2026, 10, 22, 13, 25, 0)


def _stale_line(ts, age, after=0, tf=1):
    return (f"{ts:%Y-%m-%d %H:%M:%S},101 [WARNING] core.global_context.context: [SUMMARY STALE DROP] global lag "
            f"hard-drop bounded source=push tf={tf} label=merged_get before=59 after={after} max_age_sec=900 "
            f"grace_sec=60 latest_age_sec={age} latest_dt=2026-10-22 12:55:00")


def test_parse_takes_recent_tf1_after0_only():
    text = "\n".join([
        _stale_line(NOW - dt.timedelta(seconds=30), 1809.1),
        _stale_line(NOW - dt.timedelta(seconds=20), 50.0, after=55),    # 残っている = 古くない
        _stale_line(NOW - dt.timedelta(seconds=10), 30.0, tf=5),        # 5 分足は見ない
        _stale_line(NOW - dt.timedelta(seconds=600), 10.0),             # 古い行は見ない
        "garbage",
    ])
    assert WD.parse_main_summary_stale(text, NOW) == 1809.1
    assert WD.parse_main_summary_stale("", NOW) is None


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(WD, "RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(WD, "LOG_PATH", str(tmp_path / "watchdog.log"))
    monkeypatch.setattr(WD, "_alert", lambda *a, **k: None)
    monkeypatch.setattr(WD.time, "sleep", lambda s: None)
    calls = {"reset": 0, "restart": []}

    def fake_reset():
        calls["reset"] += 1
        return ["X:->ok"]

    monkeypatch.setattr(WD, "reset_nas_drives", fake_reset)
    monkeypatch.setattr(WD.subprocess, "Popen", lambda args, **k: calls["restart"].append(args))
    return calls


def test_reset_after_two_probe_failures_with_cooldown(env, monkeypatch):
    state = {"ok": False}
    monkeypatch.setattr(WD, "check_nas_probe", lambda now: (state["ok"], "nas open failed OSError: [Errno 24]"))
    monkeypatch.setattr(WD, "_stale_window", lambda now: False)
    g = WD.NasGuard()
    g.step(NOW)
    assert env["reset"] == 0                      # 1 回目の失敗ではまだ触らない
    g.step(NOW + dt.timedelta(seconds=30))
    assert env["reset"] == 1
    g.step(NOW + dt.timedelta(seconds=60))
    assert env["reset"] == 1                      # 3 分は空ける
    g.step(NOW + dt.timedelta(minutes=4))
    assert env["reset"] == 2


def test_restart_only_when_main_alone_is_stale_for_10min(env, monkeypatch):
    monkeypatch.setattr(WD, "check_nas_probe", lambda now: (True, "nas ok"))
    monkeypatch.setattr(WD, "_stale_window", lambda now: True)
    monkeypatch.setattr(WD, "check_main_summary_stale", lambda now: (False, "main-only stale"))
    g = WD.NasGuard()
    g.step(NOW)
    g.step(NOW + dt.timedelta(minutes=9))
    assert env["restart"] == []
    g.step(NOW + dt.timedelta(minutes=10))
    assert len(env["restart"]) == 1 and env["restart"][0][-2:] == ["-Reason", "main_summary_stale_10min"]
    assert WD.nas_restart_count(NOW) == 1


def test_restart_capped_per_day_and_can_be_disabled(env, monkeypatch):
    monkeypatch.setattr(WD, "check_nas_probe", lambda now: (True, "nas ok"))
    monkeypatch.setattr(WD, "_stale_window", lambda now: True)
    monkeypatch.setattr(WD, "check_main_summary_stale", lambda now: (False, "main-only stale"))
    g = WD.NasGuard()
    t = NOW
    for _ in range(4):                            # 10 分ずつ古いまま続く
        g.step(t)
        t += dt.timedelta(minutes=10)
        g.step(t)
        t += dt.timedelta(minutes=1)
    assert len(env["restart"]) == 2               # 1 日 2 回まで
    monkeypatch.setenv("WATCHDOG_NAS_RESTART", "0")
    g2 = WD.NasGuard()
    monkeypatch.setattr(WD, "nas_restart_count", lambda now: 0)
    g2.step(t)
    g2.step(t + dt.timedelta(minutes=10))
    assert len(env["restart"]) == 2


def test_main_stale_ignored_when_nas_summary_also_stale(monkeypatch, tmp_path):
    log = tmp_path / "console_20261022_113127_1.log"
    log.write_text(_stale_line(NOW - dt.timedelta(seconds=10), 1809.1) + "\n", encoding="utf-8")
    old = NOW.timestamp() - 3600
    os.utime(log, (old, old))
    monkeypatch.setattr(WD, "newest_main_console_log", lambda now: str(log))
    monkeypatch.setattr(WD.os.path, "getctime", lambda p: old)
    monkeypatch.setattr(WD.time, "time", lambda: NOW.timestamp())
    monkeypatch.setattr(WD, "nas_summary_age_min", lambda now: 30.0)     # データ自体が来ていない
    ok, msg = WD.check_main_summary_stale(NOW)
    assert ok and "not main-only" in msg
    monkeypatch.setattr(WD, "nas_summary_age_min", lambda now: 0.5)      # NAS は進んでいる
    ok, msg = WD.check_main_summary_stale(NOW)
    assert not ok and "1809s" in msg


def test_probe_reads_sqlite_header(monkeypatch, tmp_path):
    db = tmp_path / "summary20261022.db"
    db.write_bytes(b"SQLite format 3\x00" + b"\x00" * 100)
    monkeypatch.setattr(WD, "nas_summary_db_path", lambda now: str(db))
    assert WD.check_nas_probe(NOW) == (True, "nas ok")
    db.unlink()
    assert WD.check_nas_probe(NOW)[0] is True     # まだ作られていないだけ(フォルダは読める)
    monkeypatch.setattr(WD, "nas_summary_db_path", lambda now: str(tmp_path / "missing_dir" / "x.db"))
    assert WD.check_nas_probe(NOW)[0] is False
