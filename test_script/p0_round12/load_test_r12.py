"""第12ラウンドの before/after 実測.

実注文なし。NAS 上の本番 DB は読み取り専用のみ。

測る:
  1. ログ 1 行あたりの NAS コスト (同期 flush vs 非同期キュー)
  2. 実ログ (console_20260828_084851) の構成から、表ダンプ除去の効果
  3. PUSH DB read: 毎回 open+schema+close vs 接続再利用
  4. PUSH DB read: full vs 増分カーソル
  5. long_tf: date(col)=? vs 範囲述語
  6. 08:51 の 50 秒 tick を、ログ由来の実コストで再構成した before/after
"""
from __future__ import annotations

import datetime as dt
import io
import os
import re
import sqlite3
import statistics
import sys
import tempfile
import threading
import time

ROOT = os.environ.get(
    "KABU_SRC_ROOT",
    r"W:\BUSINESS\事業\04_株式自動売買システム\ソースコード",
)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.environ["AUTOSTOCK_DRY_RUN"] = "1"
os.environ["ALLOW_ORDERS"] = "0"

NAS_LOG_DIR = os.path.join("X:" + os.sep, "Logs", "console")
PUSH_DB = os.path.join("X:" + os.sep, "raw_data", "kabu_station", "push", "push20260828.db")
SUMMARY_DB = os.path.join("X:" + os.sep, "raw_data", "kabu_station", "summary", "summary20260828.db")
LOGFILE = os.environ.get("R12_LOGFILE", "")

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
# 1. ログ書き込みコスト
# ============================================================

def bench_log_write() -> None:
    section("1. ログ 1 行あたりの NAS コスト: 同期 flush vs 非同期キュー")
    from core.logging import console_tee as ct

    os.makedirs(NAS_LOG_DIR, exist_ok=True)
    sync_path = os.path.join(NAS_LOG_DIR, "_bench_sync.log")
    async_path = os.path.join(NAS_LOG_DIR, "_bench_async.log")
    for p in (sync_path, async_path):
        try:
            os.remove(p)
        except FileNotFoundError:
            pass

    N = 400
    line = "2026-08-28 08:51:00,000 [INFO] core.global_context.context: " + ("x" * 140) + "\n"

    # --- before: buffering=1 + 毎回 flush (旧 console_tee) ---
    f = open(sync_path, "a", encoding="utf-8", errors="replace", buffering=1)
    ts = []
    for _ in range(N):
        t0 = time.perf_counter()
        f.write(line)
        f.flush()
        ts.append((time.perf_counter() - t0) * 1000)
    f.close()
    ts.sort()
    sync_mean = statistics.mean(ts)
    sync_total = sum(ts) / 1000.0
    print(f"    before (sync flush) : mean={sync_mean:7.3f}ms p95={ts[int(N * .95)]:7.3f} "
          f"max={ts[-1]:8.3f} total={sync_total:6.2f}s")

    # --- after: 非同期キュー ---
    f2 = open(async_path, "a", encoding="utf-8", errors="replace", buffering=1024 * 64)
    w = ct._AsyncFileWriter(f2, maxsize=200000, flush_interval=0.2)
    ts2 = []
    for _ in range(N):
        t0 = time.perf_counter()
        w.write(line)
        ts2.append((time.perf_counter() - t0) * 1000)
    caller_total = sum(ts2) / 1000.0
    deadline = time.time() + 30
    while time.time() < deadline and w.stats()["queued"] > 0:
        time.sleep(0.05)
    w.close()
    f2.close()
    ts2.sort()
    async_mean = statistics.mean(ts2)
    print(f"    after  (async queue): mean={async_mean:7.3f}ms p95={ts2[int(N * .95)]:7.3f} "
          f"max={ts2[-1]:8.3f} total={caller_total:6.2f}s")

    speedup = sync_mean / max(async_mean, 1e-6)
    print(f"    -> 呼び出しスレッドから見て {speedup:.0f}x 速い "
          f"({sync_total:.2f}s -> {caller_total:.3f}s / {N} 行)")
    check("非同期化で呼び出し側のログコストがほぼ消える",
          caller_total < sync_total / 20, f"{sync_total:.2f}s -> {caller_total:.3f}s")

    sz = os.path.getsize(async_path) if os.path.exists(async_path) else 0
    check("非同期でも全行がファイルに書かれている", sz >= N * len(line) * 0.9,
          f"bytes={sz}")
    for p in (sync_path, async_path):
        try:
            os.remove(p)
        except FileNotFoundError:
            pass
    return sync_mean


# ============================================================
# 2. 実ログ構成からの効果推定
# ============================================================

def estimate_from_real_log(per_line_ms: float) -> None:
    section("2. 実ログ (2026-08-28 08:48-08:55) の構成から表ダンプ除去の効果を推定")
    if not LOGFILE or not os.path.exists(LOGFILE):
        print("    skip: R12_LOGFILE が指定されていない")
        return
    pat = re.compile(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d+) \[(\w+)\] ([\w.]+): (.*)')
    lines = io.open(LOGFILE, encoding="utf-8", errors="replace").read().splitlines(True)
    total_lines = len(lines)
    table_lines = 0
    table_bytes = 0
    i = 0
    while i < len(lines):
        m = pat.match(lines[i])
        if m:
            j = i + 1
            extra = 0
            eb = 0
            while j < len(lines) and not pat.match(lines[j]):
                extra += 1
                eb += len(lines[j].encode("utf-8", "replace"))
                j += 1
            if extra >= 3 and m.group(3) == "core.global_context.context":
                table_lines += extra + 1
                table_bytes += eb
            i = j
        else:
            i += 1
    total_bytes = os.path.getsize(LOGFILE)
    print(f"    ログ全体          : {total_lines:,} 行 / {total_bytes:,} B")
    print(f"    表ダンプ由来      : {table_lines:,} 行 / {table_bytes:,} B "
          f"({100 * table_bytes / total_bytes:.1f}%)")
    before_s = total_lines * per_line_ms / 1000.0
    after_lines = total_lines - table_lines
    print(f"    NAS 同期書き込み推定: before {before_s:6.1f}s "
          f"(全 {total_lines:,} 行 x {per_line_ms:.2f}ms)")
    print(f"                          表除去のみ {after_lines * per_line_ms / 1000.0:6.1f}s "
          f"({after_lines:,} 行)")
    print(f"                          + 非同期化  ~0s (呼び出しスレッドから見て)")
    check("表ダンプがログ量の過半を占めていた", table_bytes > total_bytes * 0.5,
          f"{100 * table_bytes / total_bytes:.1f}%")


# ============================================================
# 3. PUSH DB read: 接続再利用
# ============================================================

def bench_push_db_conn() -> None:
    section("3. PUSH DB read: 毎回 open+schema+close vs 接続再利用 (NAS 実測)")
    if not os.path.exists(PUSH_DB):
        print(f"    skip: {PUSH_DB} が無い")
        return
    size_mb = os.path.getsize(PUSH_DB) / 1024 / 1024
    print(f"    db={os.path.basename(PUSH_DB)} size={size_mb:.1f}MB")

    def current(n=8):
        ts = []
        for _ in range(n):
            t0 = time.perf_counter()
            os.path.exists(PUSH_DB)
            c = sqlite3.connect(PUSH_DB, timeout=3.0)
            c.execute("PRAGMA query_only=ON")
            c.execute("PRAGMA busy_timeout=3000")
            c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            c.execute('PRAGMA table_info("stream_data_raw")').fetchall()
            mx = c.execute("SELECT MAX(rowid) FROM stream_data_raw").fetchone()[0] or 0
            c.execute("SELECT rowid,* FROM stream_data_raw WHERE rowid > ? ORDER BY rowid LIMIT 4000",
                      (mx - 30,)).fetchall()
            c.close()
            ts.append((time.perf_counter() - t0) * 1000)
        return ts

    def reused(n=8):
        c = sqlite3.connect(PUSH_DB, timeout=3.0, check_same_thread=False)
        c.execute("PRAGMA query_only=ON")
        c.execute("PRAGMA busy_timeout=3000")
        ts = []
        try:
            for _ in range(n):
                t0 = time.perf_counter()
                mx = c.execute("SELECT MAX(rowid) FROM stream_data_raw").fetchone()[0] or 0
                c.execute("SELECT rowid,* FROM stream_data_raw WHERE rowid > ? ORDER BY rowid LIMIT 4000",
                          (mx - 30,)).fetchall()
                ts.append((time.perf_counter() - t0) * 1000)
        finally:
            c.close()
        return ts

    a = current()
    b = reused()
    ma, mb = statistics.mean(a), statistics.mean(b)
    print(f"    before (open+schema+close): mean={ma:8.1f}ms max={max(a):8.1f}ms")
    print(f"    after  (connection reuse) : mean={mb:8.1f}ms max={max(b):8.1f}ms")
    print(f"    -> {ma / max(mb, 0.01):.1f}x")
    check("接続再利用で PUSH DB read が大幅に速くなる", mb < ma / 3.0,
          f"{ma:.1f}ms -> {mb:.1f}ms")


# ============================================================
# 4. 増分カーソル
# ============================================================

def bench_incremental() -> None:
    section("4. PUSH DB read: full read vs 増分カーソル")
    import scheduler_jobs.summary.memory_latest_1m as ml
    from pathlib import Path

    tmp = tempfile.mkdtemp(prefix="incbench_")
    db = os.path.join(tmp, "push20260828.db")
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE stream_data_raw (symbol TEXT, symbolname TEXT, datetime TEXT, price REAL, volume REAL)")
    base = dt.datetime.now()
    c.executemany(
        "INSERT INTO stream_data_raw VALUES (?,?,?,?,?)",
        [(str(7000 + i % 50), "T", (base - dt.timedelta(seconds=20000 - i)).isoformat(), 100.0, 1.0)
         for i in range(20000)],
    )
    c.commit()
    c.close()

    ml._push_db_path = lambda: Path(db)         # type: ignore[assignment]
    os.environ["SUMMARY_MAIN_PUSH_DB_SHARE_TTL_SEC"] = "0"
    os.environ["SUMMARY_MAIN_PUSH_DB_REFRESH_LIMIT"] = "20000"
    os.environ["SUMMARY_MAIN_PUSH_DB_RING_MAX_ROWS"] = "30000"

    # full
    os.environ["SUMMARY_MAIN_PUSH_DB_INCREMENTAL"] = "0"
    ml.reset_push_db_cursor()
    t0 = time.perf_counter()
    for _ in range(5):
        ml._read_push_db_recent()
    full_s = (time.perf_counter() - t0) / 5

    # incremental
    os.environ["SUMMARY_MAIN_PUSH_DB_INCREMENTAL"] = "1"
    ml.reset_push_db_cursor()
    ml._read_push_db_recent()          # prime
    conn = sqlite3.connect(db)
    ts = []
    for _ in range(5):
        conn.executemany(
            "INSERT INTO stream_data_raw VALUES (?,?,?,?,?)",
            [(str(7000 + i % 50), "T", dt.datetime.now().isoformat(), 100.0, 1.0) for i in range(60)],
        )
        conn.commit()
        t0 = time.perf_counter()
        ml._read_push_db_recent()
        ts.append(time.perf_counter() - t0)
    conn.close()
    inc_s = statistics.mean(ts)

    print(f"    full read (20,000 行毎回): {full_s * 1000:8.1f}ms")
    print(f"    増分 (60 行/回)          : {inc_s * 1000:8.1f}ms   -> {full_s / max(inc_s, 1e-6):.1f}x")
    check("増分カーソルで PUSH DB read が速くなる", inc_s < full_s,
          f"{full_s * 1000:.1f}ms -> {inc_s * 1000:.1f}ms")

    for k in ("SUMMARY_MAIN_PUSH_DB_SHARE_TTL_SEC", "SUMMARY_MAIN_PUSH_DB_REFRESH_LIMIT",
              "SUMMARY_MAIN_PUSH_DB_RING_MAX_ROWS", "SUMMARY_MAIN_PUSH_DB_INCREMENTAL"):
        os.environ.pop(k, None)
    ml.reset_push_db_cursor()


# ============================================================
# 5. long_tf 範囲述語
# ============================================================

def bench_long_tf() -> None:
    section("5. long_tf: date(col)=? vs 範囲述語 (NAS 実測)")
    if not os.path.exists(SUMMARY_DB):
        print(f"    skip: {SUMMARY_DB} が無い")
        return
    c = sqlite3.connect(SUMMARY_DB, timeout=8.0)
    c.execute("PRAGMA query_only=ON")
    tbl = "stock_summary_3min"
    try:
        n = c.execute(f'SELECT COUNT(*) FROM "{tbl}"').fetchone()[0]
    except Exception:
        print(f"    skip: {tbl} が無い")
        c.close()
        return
    today = dt.date.today().isoformat()
    tom = (dt.date.today() + dt.timedelta(days=1)).isoformat()
    maxa = (dt.datetime.now() + dt.timedelta(seconds=60)).strftime("%Y-%m-%d %H:%M:%S")
    latest = c.execute(f'SELECT MAX("datetime") FROM "{tbl}"').fetchone()[0]
    start = (dt.datetime.fromisoformat(str(latest)[:19]) - dt.timedelta(minutes=12)).strftime("%Y-%m-%d %H:%M:%S") if latest else today + " 00:00:00"

    def bench(sql, params, n=6):
        ts = []
        for _ in range(n):
            t0 = time.perf_counter()
            c.execute(sql, params).fetchall()
            ts.append((time.perf_counter() - t0) * 1000)
        return statistics.mean(ts)

    a = bench(f'SELECT * FROM "{tbl}" WHERE "datetime">=? AND date("datetime")=? AND "datetime"<=? ORDER BY "datetime" DESC LIMIT ?',
              (start, today, maxa, 4000))
    b = bench(f'SELECT * FROM "{tbl}" WHERE "datetime">=? AND "datetime"<? AND "datetime"<=? ORDER BY "datetime" DESC LIMIT ?',
              (start, tom + " 00:00:00", maxa, 4000))
    plan = c.execute(f'EXPLAIN QUERY PLAN SELECT * FROM "{tbl}" WHERE "datetime">=? AND "datetime"<?', (start, tom + " 00:00:00")).fetchall()
    c.close()
    print(f"    rows={n:,}")
    print(f"    before date(col)=? : {a:8.1f}ms")
    print(f"    after  範囲述語     : {b:8.1f}ms   -> {a / max(b, 0.01):.1f}x")
    print(f"    plan: {plan}")
    check("範囲述語のほうが速い (index が効く)", b <= a, f"{a:.1f}ms -> {b:.1f}ms")


# ============================================================
# 6. 08:51 tick の再構成
# ============================================================

def reconstruct_tick(per_line_ms: float) -> None:
    section("6. 08:51 の 50 秒 tick を実コストで再構成")
    if not LOGFILE or not os.path.exists(LOGFILE):
        print("    skip: R12_LOGFILE が指定されていない")
        return
    pat = re.compile(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d+) \[(\w+)\] ([\w.]+): (.*)')
    w0 = dt.datetime(2026, 8, 28, 8, 51, 0)
    w1 = dt.datetime(2026, 8, 28, 8, 51, 52)
    lines = io.open(LOGFILE, encoding="utf-8", errors="replace").read().splitlines(True)
    ctx_records = 0
    ctx_physical_lines = 0
    ctx_table_lines = 0
    i = 0
    while i < len(lines):
        m = pat.match(lines[i])
        if m:
            t = dt.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S,%f")
            j = i + 1
            extra = 0
            while j < len(lines) and not pat.match(lines[j]):
                extra += 1
                j += 1
            if w0 <= t <= w1 and m.group(3) == "core.global_context.context":
                ctx_records += 1
                ctx_physical_lines += 1 + extra
                if extra >= 3:
                    ctx_table_lines += extra
            i = j
        else:
            i += 1
    print(f"    08:51 窓の core.global_context.context: {ctx_records} レコード / "
          f"{ctx_physical_lines} 物理行 (うち表 {ctx_table_lines} 行)")
    print(f"    ログ実測での占有            : 23.5s / 53.1s = 44.3%")
    print(f"    NAS 同期書き込み推定 before : {ctx_physical_lines * per_line_ms / 1000.0:.1f}s")
    print(f"    表除去後 (同期のまま)       : "
          f"{(ctx_physical_lines - ctx_table_lines) * per_line_ms / 1000.0:.1f}s")
    print(f"    + 非同期化                  : 呼び出しスレッドから見て ~0s")
    check("表除去で 08:51 窓のログ物理行が大幅に減る",
          ctx_table_lines > ctx_physical_lines * 0.5,
          f"{ctx_table_lines}/{ctx_physical_lines}")


def main() -> int:
    per_line = bench_log_write() or 10.0
    estimate_from_real_log(per_line)
    bench_push_db_conn()
    bench_incremental()
    bench_long_tf()
    reconstruct_tick(per_line)

    section("SUMMARY")
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"total={len(RESULTS)} passed={passed} failed={len(failed)}")
    for n in failed:
        print("  FAILED:", n)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
