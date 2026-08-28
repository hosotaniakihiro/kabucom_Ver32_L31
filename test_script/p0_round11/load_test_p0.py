"""P0 修正の負荷試験 + fault injection.

実注文・実返済注文は一切行わない。DB 障害は mock で注入する。

測定:
  1. positions read を 6 系統 x 高頻度で叩き、実 DB read 回数と
     POSITIONS_STATE の一貫性を見る (P0-3 query storm = 0)
  2. その途中で disk I/O error を注入し、
     - ENTRY が fail-open しないこと
     - EXIT が建玉を 0 件に落とさないこと
     - リトライストームにならないこと
  3. realtime history guard を 22,851 行 x 281 列でかけたときの所要時間
  4. runtime_state_store の連続保存でハンドルが増えないこと
  5. NAS 上の positions.db へ実際に高頻度アクセスして disk I/O error が
     出ないことを確認する (読み取り専用のコピー DB)
"""
from __future__ import annotations

import ctypes
import datetime as dt
import os
import shutil
import sqlite3
import statistics
import sys
import tempfile
import threading
import time
from collections import Counter

ROOT = os.environ.get(
    "KABU_SRC_ROOT",
    r"W:\BUSINESS\事業\04_株式自動売買システム\ソースコード",
)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.environ["AUTOSTOCK_DRY_RUN"] = "1"
os.environ["ALLOW_ORDERS"] = "0"

RESULTS: list = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def section(t: str) -> None:
    print()
    print("=" * 72)
    print(t)
    print("=" * 72)


def handles() -> int:
    try:
        from ctypes import wintypes

        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.GetProcessHandleCount.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        k.GetProcessHandleCount.restype = wintypes.BOOL
        k.GetCurrentProcess.restype = wintypes.HANDLE
        n = wintypes.DWORD(0)
        if k.GetProcessHandleCount(k.GetCurrentProcess(), ctypes.byref(n)):
            return int(n.value)
    except Exception:
        pass
    return -1


# ============================================================
# 1 + 2. positions 6 系統 高頻度 + fault injection
# ============================================================

def load_positions_consumers(duration: float = 20.0, fault_from: float = 7.0,
                             fault_until: float = 14.0) -> None:
    section("負荷試験 1/2: positions 6 系統 高頻度 + disk I/O error 注入")

    import trading.position.position_state_service as pss

    os.environ["POSITION_STATE_CACHE_TTL_SEC"] = "1.0"
    os.environ["POSITION_STATE_DB_BACKOFF_SEC"] = "1,2,5,10,30"

    t_start = time.perf_counter()
    db_calls = Counter()
    broker_calls = Counter()
    lock = threading.Lock()

    def in_fault() -> bool:
        el = time.perf_counter() - t_start
        return fault_from <= el < fault_until

    def fake_db():
        with lock:
            db_calls["total"] += 1
        if in_fault():
            with lock:
                db_calls["error"] += 1
            raise sqlite3.OperationalError("disk I/O error")
        with lock:
            db_calls["ok"] += 1
        return {"7203": {"symbol": "7203", "qty": 100, "entry_price": 2500.0, "side": "BUY"}}

    def fake_broker():
        with lock:
            broker_calls["total"] += 1
        # broker は常に正常 (ケース B/C 相当)
        return ({"7203": {"symbol": "7203", "qty": 100, "entry_price": 2500.0, "side": "BUY"}},
                True, "")

    pss._read_db_positions_raising = fake_db
    pss._read_broker_positions = fake_broker
    pss.reset_for_test()

    # 本番の 6 系統を模す
    consumers = [
        "trading.position.open_position_sync",
        "core.position_sync",
        "kabu_api.positions",
        "trading.exit.executor",
        "entry.precheck",
        "scheduler.exit_loop_5s",
    ]
    stop = threading.Event()
    observations: list = []
    obs_lock = threading.Lock()
    calls = Counter()

    def worker(name: str, period: float) -> None:
        while not stop.is_set():
            st = pss.get_positions_state(caller=name)
            with obs_lock:
                calls[name] += 1
                observations.append((time.perf_counter() - t_start, name,
                                     st.positions_known, st.entry_allowed,
                                     st.count, st.source_used, st.db_ok))
            time.sleep(period)

    threads = []
    for i, name in enumerate(consumers):
        # 1 秒ループ相当 (本番の exit_loop / precheck は毎秒)
        t = threading.Thread(target=worker, args=(name, 0.05 + i * 0.01), daemon=True)
        t.start()
        threads.append(t)

    time.sleep(duration)
    stop.set()
    for t in threads:
        t.join(5)

    total_consumer_calls = sum(calls.values())
    print(f"    consumer calls total = {total_consumer_calls}  ({dict(calls)})")
    print(f"    actual DB reads      = {db_calls['total']} (ok={db_calls['ok']} err={db_calls['error']})")
    print(f"    actual broker reads  = {broker_calls['total']}")

    # 1 秒 TTL なので DB read は概ね duration 回 + backoff 分の減少
    check(f"consumer {total_consumer_calls} 回 -> DB read {db_calls['total']} 回 (query storm なし)",
          db_calls["total"] <= duration + 5,
          f"db_reads={db_calls['total']} <= {int(duration) + 5}")
    check("障害中に毎秒リトライしていない (backoff が効いている)",
          db_calls["error"] <= 6, f"errors={db_calls['error']}")
    check("DB 障害中も broker の安全確認は継続",
          broker_calls["total"] >= duration * 0.8,
          f"broker_reads={broker_calls['total']}")

    # 建玉が 0 件に落ちた瞬間が無いこと
    zero_count = [o for o in observations if o[4] == 0]
    check("建玉が 0 件に落ちた観測が 1 度も無い",
          len(zero_count) == 0, f"zero_observations={len(zero_count)}")

    # 障害中も entry_allowed が正しい (broker OK なので True で良い)
    fault_obs = [o for o in observations if fault_from <= o[0] < fault_until]
    unknown_but_allowed = [o for o in fault_obs if (not o[2]) and o[3]]
    check("状態 unknown なのに entry_allowed=True の観測は 0",
          len(unknown_but_allowed) == 0, f"violations={len(unknown_but_allowed)}")
    print(f"    障害区間の観測 {len(fault_obs)} 件、"
          f"source_used={set(o[5] for o in fault_obs)}")

    s = pss.stats()
    print(f"    stats: {s}")
    check("独立障害エピソード数 = 1 (連続失敗を 1 件に畳めている)",
          s["db_error_episodes"] == 1, str(s["db_error_episodes"]))

    os.environ.pop("POSITION_STATE_CACHE_TTL_SEC", None)
    os.environ.pop("POSITION_STATE_DB_BACKOFF_SEC", None)


# ============================================================
# 2b. broker も落とす (ケース D) 負荷
# ============================================================

def load_case_d() -> None:
    section("負荷試験 2b: DB も broker も NG (ケース D) を 8 秒間")
    import trading.position.position_state_service as pss

    os.environ["POSITION_STATE_CACHE_TTL_SEC"] = "0.5"

    # 先に last-good を作る
    pss._read_db_positions_raising = lambda: {
        "6758": {"symbol": "6758", "qty": 200, "entry_price": 3000.0, "side": "BUY"}}
    pss._read_broker_positions = lambda: (
        {"6758": {"symbol": "6758", "qty": 200, "entry_price": 3000.0, "side": "BUY"}}, True, "")
    pss.reset_for_test()
    st = pss.get_positions_state(force=True, caller="load.d.warmup")
    check("warmup で last-good を作れた", st.count == 1 and st.positions_known,
          f"count={st.count}")

    def boom_db():
        raise sqlite3.OperationalError("disk I/O error")

    def boom_broker():
        return {}, False, "connection refused"

    pss._read_db_positions_raising = boom_db
    pss._read_broker_positions = boom_broker

    stop = threading.Event()
    viol = Counter()
    obs = Counter()

    def w(name):
        while not stop.is_set():
            s = pss.get_positions_state(caller=name)
            obs["n"] += 1
            # 発注ゲートそのもの。ここが False であることが安全条件。
            if pss.entry_allowed(caller=name):
                viol["entry_gate_open"] += 1
            # 参考: キャッシュ経由のフィールド (齢による降格が効いているか)
            if s.entry_allowed:
                viol["cached_field_true"] += 1
            if s.count == 0:
                viol["positions_zeroed"] += 1
            time.sleep(0.03)

    ts = [threading.Thread(target=w, args=(f"c{i}",), daemon=True) for i in range(6)]
    for t in ts:
        t.start()
    time.sleep(8)
    stop.set()
    for t in ts:
        t.join(5)

    print(f"    観測 {obs['n']} 件 "
          f"(cached_field_true={viol['cached_field_true']} = 障害直前 TTL 1 回分)")
    check("D: ENTRY ゲートが一度も開かなかった (fail-closed)",
          viol["entry_gate_open"] == 0, f"violations={viol['entry_gate_open']}")
    check("D: 建玉が一度も 0 件になっていない (last-known 保持)",
          viol["positions_zeroed"] == 0, f"violations={viol['positions_zeroed']}")

    os.environ.pop("POSITION_STATE_CACHE_TTL_SEC", None)


# ============================================================
# 3. realtime history guard の所要時間
# ============================================================

def load_history_guard() -> None:
    section("負荷試験 3: realtime history guard 22,851 rows x 281 cols")
    import numpy as np
    import pandas as pd
    from trading.summary import realtime_history_guard as g

    g.reset_stats()
    now = pd.Timestamp.now().normalize() + pd.Timedelta(hours=14)
    n_sym, n_bar = 349, 66
    rows = n_sym * n_bar
    data = {
        "symbol": np.repeat([f"{1000 + i}" for i in range(n_sym)], n_bar),
        "datetime": np.tile(
            [now - pd.Timedelta(minutes=n_bar - 1 - k) for k in range(n_bar)], n_sym),
    }
    for c in range(279):
        data[f"col{c}"] = np.random.rand(rows)
    hist = pd.DataFrame(data)
    print(f"    frame: rows={len(hist)} cols={len(hist.columns)}")

    times = []
    for _ in range(5):
        t0 = time.perf_counter()
        out = g.to_latest_projection(hist, caller="loadtest", interval=1)
        times.append(time.perf_counter() - t0)
    med = statistics.median(times)
    print(f"    projection times: {[round(t, 4) for t in times]}  median={med:.4f}s")

    check(f"22,{len(hist) - 22000} 行 -> {len(out)} 行", len(out) == n_sym, f"rows={len(out)}")
    check(f"projection が 1 tick 予算内 (median {med:.3f}s < 1.0s)", med < 1.0, f"{med:.4f}s")

    # 素通しの場合のオーバーヘッド
    small = out.copy()
    t0 = time.perf_counter()
    for _ in range(100):
        g.to_latest_projection(small, caller="loadtest.small")
    passthru = (time.perf_counter() - t0) / 100
    print(f"    349 行素通し 1 回あたり {passthru * 1000:.3f} ms")
    check("既に projection 済みなら実質ゼロコスト", passthru < 0.001,
          f"{passthru * 1000:.3f}ms")


# ============================================================
# 4. runtime_state_store 連続保存
# ============================================================

def load_runtime_state() -> None:
    section("負荷試験 4: runtime_state_store 連続保存 2000 回 (ローカル一時DB)")
    from trading.runtime_persistence import runtime_state_store as rss

    tmp = tempfile.mkdtemp(prefix="rss_load_")
    rss.BASE_DIR = tmp
    rss._SCHEMA_DONE.clear()
    rss.ensure_runtime_state_db("20260827")

    import gc

    gc.collect()
    h0 = handles()
    c0 = rss.connection_stats()

    t0 = time.perf_counter()
    N = 2000
    for i in range(N):
        rss.save_position_state(
            symbol=f"{7000 + (i % 20)}", side="BUY",
            entry_time=dt.datetime(2026, 8, 27, 13, 0, 0),
            entry_price=100.0, current_price=101.0,
            highest_since_entry=102.0, lowest_since_entry=99.0,
            qty=100, status="OPEN", trade_date="20260827",
        )
    el = time.perf_counter() - t0
    gc.collect()
    h1 = handles()
    c1 = rss.connection_stats()

    print(f"    {N} 回 / {el:.2f}s = {N / el:.0f} 回/s")
    print(f"    conn opened={c1['opened'] - c0['opened']} outstanding={c1['outstanding']}")
    print(f"    handles {h0} -> {h1}")
    check("outstanding == 0", c1["outstanding"] == 0, str(c1["outstanding"]))
    if h0 > 0 and h1 > 0:
        check(f"ハンドル増加 {h1 - h0} <= 20 (旧実装は 2000 回で +6000)",
              h1 - h0 <= 20, f"{h0} -> {h1}")


# ============================================================
# 5. NAS positions.db 実アクセス負荷
# ============================================================

def load_nas_positions() -> None:
    section("負荷試験 5: NAS positions.db へ実アクセス (読み取り専用コピー)")
    base = os.environ.get("REPRO_POSITIONS_DIR",
                          os.path.join("X:" + os.sep, "trading", "runtime", "positions"))
    src = os.path.join(base, "positions.db")
    if not os.path.exists(src):
        print(f"    skip: {src} が無い")
        return

    dst = os.path.join(base, "_loadtest_positions.db")
    for s in ("", "-wal", "-shm", "-journal"):
        try:
            os.remove(dst + s)
        except FileNotFoundError:
            pass
    shutil.copyfile(src, dst)

    stop = threading.Event()
    res = Counter()
    lock = threading.Lock()
    h0 = handles()

    def w():
        while not stop.is_set():
            conn = None
            try:
                conn = sqlite3.connect(dst, timeout=30, check_same_thread=False)
                cur = conn.cursor()
                cur.execute("PRAGMA busy_timeout=60000;")
                cur.execute("PRAGMA temp_store=MEMORY;")
                cur.execute("PRAGMA synchronous=NORMAL;")
                cur.execute("PRAGMA foreign_keys=ON;")
                cur.execute("SELECT id, symbol, status FROM positions WHERE status='OPEN'").fetchall()
                cur.execute("SELECT id, symbol, status FROM positions WHERE status='CLOSING'").fetchall()
                cur.close()
                with lock:
                    res["ok"] += 1
            except Exception as e:  # noqa: BLE001
                with lock:
                    res["err"] += 1
                    res[f"{type(e).__name__}:{str(e)[:40]}"] += 1
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass

    ts = [threading.Thread(target=w, daemon=True) for _ in range(12)]
    t0 = time.perf_counter()
    for t in ts:
        t.start()
    time.sleep(30)
    stop.set()
    for t in ts:
        t.join(10)
    el = time.perf_counter() - t0
    h1 = handles()

    kinds = {k: v for k, v in res.items() if k not in ("ok", "err")}
    print(f"    {el:.1f}s / 12 threads: ok={res['ok']} err={res['err']} "
          f"({res['ok'] / el:.0f} q/s)  handles {h0} -> {h1}")
    if kinds:
        print(f"    error kinds: {kinds}")
    check("NAS positions.db 高頻度アクセスで disk I/O error = 0",
          res["err"] == 0, f"errors={res['err']} kinds={kinds}")
    if h0 > 0 and h1 > 0:
        check("接続を閉じていればハンドルは増えない", h1 - h0 <= 30, f"{h0} -> {h1}")

    for s in ("", "-wal", "-shm", "-journal"):
        try:
            os.remove(dst + s)
        except FileNotFoundError:
            pass


def main() -> int:
    load_positions_consumers()
    load_case_d()
    load_history_guard()
    load_runtime_state()
    load_nas_positions()

    section("SUMMARY")
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"total={len(RESULTS)} passed={passed} failed={len(failed)}")
    for n in failed:
        print("  FAILED:", n)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
