"""第14ラウンド修正の dry-run テスト.

実注文・実返済注文は一切行わない。NAS 本番 DB は読み取り専用。

対象:
  P0-1  子プロセスの SMB 障害検出 / 即死 backoff / パス表記統一
  P0-2  PUSH OWNER HEALTH と WS/writer/reader の切り分け
  P0-6  fresh input が無いときの即 MISS (<0.5s)
  P0-7  setter enrich スレッドを積み上げない
"""
from __future__ import annotations

import io
import logging
import os
import sqlite3
import sys
import tempfile
import threading
import time
import traceback

ROOT = os.environ.get(
    "KABU_SRC_ROOT",
    r"W:\BUSINESS\事業\04_株式自動売買システム\ソースコード",
)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
SCRIPTS = os.path.join(ROOT, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

os.environ["AUTOSTOCK_DRY_RUN"] = "1"
os.environ["ALLOW_ORDERS"] = "0"

RESULTS: list = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def section(t: str) -> None:
    print()
    print("=" * 74)
    print(t)
    print("=" * 74)


# ============================================================
# P0-1  SMB fault detection / spawn probe / fast-death backoff
# ============================================================

def test_smb_fault_classification() -> None:
    section("P0-1  SMB 障害を app 障害と区別する")
    import data_collectors_runner as dcr

    class FakeOSError(OSError):
        pass

    cases = [
        (OSError(6, "handle invalid"), False),   # winerror 属性が無い素の OSError
        (RuntimeError("python.exe: can't open file 'x.py'"), True),
        (RuntimeError("[WinError 4] Too many open files in system"), True),
        (RuntimeError("[WinError 59] unexpected network error"), True),
        (RuntimeError("[WinError 6] handle is invalid"), True),
        (RuntimeError("ValueError: bad config"), False),
    ]
    for exc, expected in cases:
        got = dcr._looks_like_smb_fault(exc)
        check(f"{type(exc).__name__}({str(exc)[:40]!r}) -> smb={expected}",
              got == expected, f"got={got}")

    e = OSError("boom")
    e.winerror = 4
    check("winerror=4 は SMB 障害", dcr._looks_like_smb_fault(e) is True)
    e2 = OSError("boom")
    e2.errno = 24
    check("errno=24 (EMFILE) は SMB 障害", dcr._looks_like_smb_fault(e2) is True)


def test_spawn_probe() -> None:
    section("P0-1  spawn 前プローブ (読めないなら spawn しない)")
    import data_collectors_runner as dcr

    logger = logging.getLogger("test.dcr")
    tmp = tempfile.mkdtemp(prefix="probe_")
    good = os.path.join(tmp, "ok.py")
    open(good, "w").write("print(1)\n")
    missing = os.path.join(tmp, "missing.py")

    from pathlib import Path

    check("読めるスクリプトは probe を通る",
          dcr._probe_paths_readable(logger, "t", Path(good)) is True)
    check("読めないスクリプトは probe で止まる",
          dcr._probe_paths_readable(logger, "t", Path(missing)) is False)

    os.environ["AUTOSTOCK_CHILD_SPAWN_PROBE"] = "0"
    try:
        check("AUTOSTOCK_CHILD_SPAWN_PROBE=0 で従来動作",
              dcr._probe_paths_readable(logger, "t", Path(missing)) is True)
    finally:
        os.environ.pop("AUTOSTOCK_CHILD_SPAWN_PROBE", None)

    # probe が失敗したら _start_child_safe は None を返し、Popen しない
    spawned = {"n": 0}
    orig_popen = dcr._popen_captured

    def counting(*a, **k):
        spawned["n"] += 1
        return orig_popen(*a, **k)

    dcr._popen_captured = counting
    try:
        r = dcr._start_child_safe(logger, "t", Path(missing))
    finally:
        dcr._popen_captured = orig_popen
    check("probe 失敗時は Popen を呼ばない", r is None and spawned["n"] == 0,
          f"popen_calls={spawned['n']}")


def test_fast_death_backoff() -> None:
    section("P0-1  即死した子は backoff の対象になる")
    import data_collectors_runner as dcr

    check("CHILD_MIN_HEALTHY_SEC が定義されている",
          isinstance(dcr.CHILD_MIN_HEALTHY_SEC, float) and dcr.CHILD_MIN_HEALTHY_SEC > 0,
          f"{dcr.CHILD_MIN_HEALTHY_SEC}")

    src = io.open(os.path.join(SCRIPTS, "data_collectors_runner.py"),
                  encoding="utf-8").read()
    check("exit ハンドラに died_fast 判定がある", "died_fast" in src)
    check("died_fast なら _schedule_retry する",
          "if died_fast:" in src and "_schedule_retry(name)" in src)
    check("生存時間で fail_counts をリセットする",
          "child healthy name=" in src)
    check("lifetime をログに出す", "lifetime=%s died_fast=%s" in src)


def test_path_normalize() -> None:
    section("P0-1  子スクリプトのパス表記をインタプリタへ揃える")
    import data_collectors_runner as dcr
    from pathlib import Path

    # 実環境で例外にならないこと / 存在するパスを返すこと
    p = dcr.PUSH_RECEIVER_RUNNER
    out = dcr._normalized_child_path(p)
    check("正規化しても存在するパスを返す", Path(out).exists(), str(out))
    exe_root = str(dcr._python_exe()).split(os.sep + ".venv" + os.sep)[0]
    check("インタプリタと同じ共有表記になる (SMB セッションを二重に持たない)",
          str(out).startswith(exe_root),
          f"exe_root={exe_root} path={out}")
    os.environ["AUTOSTOCK_CHILD_PATH_NORMALIZE"] = "0"
    try:
        check("AUTOSTOCK_CHILD_PATH_NORMALIZE=0 で元のまま",
              dcr._normalized_child_path(p) == p)
    finally:
        os.environ.pop("AUTOSTOCK_CHILD_PATH_NORMALIZE", None)


# ============================================================
# P0-2 / P0-4  PUSH OWNER HEALTH
# ============================================================

def test_push_owner_health() -> None:
    section("P0-2/P0-4  PUSH OWNER HEALTH と障害分類")
    import data_collectors.push_runtime as pr

    snap = pr._push_health_snapshot()
    for k in ("pid", "ws_connected", "ws_thread_alive", "last_recv_age",
              "last_write_age", "received_total", "written_total", "queue",
              "registered_count", "rotation", "token_tail", "db_path"):
        check(f"snapshot に {k} がある", k in snap, "")

    cases = [
        ({"last_recv_age": 1.0, "last_write_age": 100.0}, "writer_problem"),
        ({"last_recv_age": 100.0, "last_write_age": 100.0}, "websocket_or_subscription_problem"),
        ({"last_recv_age": 1.0, "last_write_age": 1.0}, "owner_ok"),
        ({"last_recv_age": None, "last_write_age": None}, "websocket_or_subscription_problem"),
    ]
    for s, expected in cases:
        got = pr._classify_push_fault(s)
        check(f"recv={s['last_recv_age']} write={s['last_write_age']} -> {expected}",
              got == expected, f"got={got}")

    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    lg = logging.getLogger("data_collectors.push_runtime")
    lg.addHandler(h)
    old = lg.level
    lg.setLevel(logging.INFO)
    try:
        pr._log_push_owner_health()
    finally:
        lg.removeHandler(h)
        lg.setLevel(old)
    out = buf.getvalue()
    check("[PUSH OWNER HEALTH] が出る", "[PUSH OWNER HEALTH]" in out, out[:120])
    check("fault 分類が出る", "fault=" in out, out[:200])


def test_push_loop_never_returns() -> None:
    section("P0-1  PUSH owner のメインループは heartbeat 失敗で抜けない")
    src = io.open(os.path.join(ROOT, "data_collectors", "push_runtime.py"),
                  encoding="utf-8").read()
    body = src.split("def run_forever(")[1]
    loop = body.split("while True:")[1]
    check("write_heartbeat が try で守られている",
          "heartbeat write failed" in loop, "")
    check("ループ本体全体が例外を飲み込む",
          "main loop iteration failed; continue" in loop, "")
    # ループ本体に「早期 return」が無いこと (行頭 return 文で判定)
    bare_returns = [ln for ln in loop.split("time.sleep(1.0)")[0].splitlines()
                    if ln.strip().startswith("return")]
    check("ループ内に早期 return が無い", not bare_returns, str(bare_returns))


# ============================================================
# P0-6  fresh input が無いときの即 MISS
# ============================================================

def test_fast_miss_when_input_stale() -> None:
    section("P0-6  fresh input が無いなら 9 秒待たず即 MISS")
    import datetime as dt

    import pandas as pd
    import scheduler_jobs.summary.runner_core as rc
    from pathlib import Path

    tmp = tempfile.mkdtemp(prefix="stalepush_")
    db = os.path.join(tmp, "push20260828.db")
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE stream_data_raw (symbol TEXT, datetime TEXT, price REAL)")
    old = (dt.datetime.now() - dt.timedelta(seconds=3600)).isoformat()
    c.executemany("INSERT INTO stream_data_raw VALUES (?,?,?)",
                  [("1301", old, 100.0)])
    c.commit()
    c.close()

    import scheduler_jobs.summary.memory_latest_1m as ml

    orig_path = ml._push_db_path
    ml._push_db_path = lambda: Path(db)
    rc._FRESH_PROBE_AT = 0.0
    rc.reset_fast_path_stats()

    slow_called = {"n": 0}
    orig_t1 = rc._job_summary_tier1_direct_memory

    def slow_t1(now, run_entry=True):
        slow_called["n"] += 1
        time.sleep(9)
        return pd.DataFrame()

    rc._job_summary_tier1_direct_memory = slow_t1
    try:
        t0 = time.perf_counter()
        out = rc.job_summary(1, display=False, now=dt.datetime.now(), run_entry=False)
        el = time.perf_counter() - t0
    finally:
        rc._job_summary_tier1_direct_memory = orig_t1
        ml._push_db_path = orig_path
        rc._FRESH_PROBE_AT = 0.0

    check(f"push DB が 1 時間古いとき {el * 1000:.0f}ms で MISS (<500ms)",
          el < 0.5, f"{el * 1000:.0f}ms")
    check("重い tier1 を呼んでいない", slow_called["n"] == 0, f"calls={slow_called['n']}")
    check("空を返す (Entry は fail-closed)", out.empty)
    st = rc.fast_path_stats()
    check("MISS として計上される", st["misses"] >= 1, str(st))

    # fresh なら従来どおり探索する
    c = sqlite3.connect(db)
    c.execute("INSERT INTO stream_data_raw VALUES (?,?,?)",
              ("1301", dt.datetime.now().isoformat(), 100.0))
    c.commit()
    c.close()
    ml._push_db_path = lambda: Path(db)
    rc._FRESH_PROBE_AT = 0.0
    try:
        reason = rc._fresh_input_hopeless(dt.datetime.now())
    finally:
        ml._push_db_path = orig_path
        rc._FRESH_PROBE_AT = 0.0
    check("fresh なら早期 MISS しない", reason == "", f"reason={reason!r}")

    # probe を無効化できる
    os.environ["SUMMARY_FRESH_INPUT_PROBE"] = "0"
    rc._FRESH_PROBE_AT = 0.0
    try:
        check("SUMMARY_FRESH_INPUT_PROBE=0 で従来動作",
              rc._fresh_input_hopeless(dt.datetime.now()) == "")
    finally:
        os.environ.pop("SUMMARY_FRESH_INPUT_PROBE", None)
        rc._FRESH_PROBE_AT = 0.0


# ============================================================
# P0-7  setter enrich スレッドを積み上げない
# ============================================================

def test_setter_enrich_no_thread_pileup() -> None:
    section("P0-7  setter enrich スレッドが積み上がらない")
    import numpy as np
    import pandas as pd
    import trading.summary.summary_controller as sc
    from global_state import GlobalDataCompat as G

    df = pd.DataFrame({
        "symbol": [str(1000 + i) for i in range(150)],
        "datetime": [pd.Timestamp("2026-08-28 12:30:00")] * 150,
        "close": np.random.rand(150) * 100,
    })

    os.environ["SUMMARY_SETTER_ENRICH_BUDGET_SEC"] = "0.2"
    os.environ["SUMMARY_SETTER_ENRICH_DEDUPE"] = "0"
    G._SETTER_ENRICH_INFLIGHT.clear()
    G._SETTER_ENRICH_STARTED.clear()

    orig = sc._enrich
    release = threading.Event()

    def stuck_enrich(d, **k):
        release.wait(30)
        return d

    sc._enrich = stuck_enrich
    before = threading.active_count()
    try:
        for _ in range(10):
            G._setter_enrich(df, 1, "unittest-pileup")
        after = threading.active_count()
        names = [t.name for t in threading.enumerate()
                 if t.name.startswith("setter-enrich-unittest-pileup")]
    finally:
        release.set()
        time.sleep(0.3)
        sc._enrich = orig
        os.environ.pop("SUMMARY_SETTER_ENRICH_BUDGET_SEC", None)
        os.environ.pop("SUMMARY_SETTER_ENRICH_DEDUPE", None)
        G._SETTER_ENRICH_INFLIGHT.clear()
        G._SETTER_ENRICH_STARTED.clear()

    check(f"10 回呼んでも enrich スレッドは {len(names)} 本 (旧実装は 10 本)",
          len(names) <= 1, f"threads={names}")
    check(f"スレッド総数の増分 {after - before} <= 2", after - before <= 2,
          f"{before} -> {after}")


def main() -> int:
    tests = [
        test_smb_fault_classification,
        test_spawn_probe,
        test_fast_death_backoff,
        test_path_normalize,
        test_push_owner_health,
        test_push_loop_never_returns,
        test_fast_miss_when_input_stale,
        test_setter_enrich_no_thread_pileup,
    ]
    for fn in tests:
        try:
            fn()
        except Exception:
            print(f"[FAIL] {fn.__name__} が例外で落ちた")
            traceback.print_exc()
            RESULTS.append((fn.__name__, False, "exception"))

    section("SUMMARY")
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"total={len(RESULTS)} passed={passed} failed={len(failed)}")
    for n in failed:
        print("  FAILED:", n)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
