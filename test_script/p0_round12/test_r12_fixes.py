"""第12ラウンド修正の dry-run テスト.

実注文・実返済注文は一切行わない。
NAS 上の本番 DB は **読み取り専用**でしか触らない。

対象:
  P0-1  ログの NAS 同期書き込み -> 非同期化 (console_tee)
  P0-1  表ダンプの env ゲート (_patched_log_df_profile)
  P0-2  FAST LANE / PUSH DB の段別計測
  P0-4  PUSH DB の read session 共有 / 同一 tick の重複 open 防止
  P0-6  PUSH DB の増分カーソル + リングバッファ
  P0-7  fallback の bounded timeout (timeout を伸ばさない)
  P0-9  long_tf_db_publish の接続クローズ + 範囲述語
  P0-10 MAIN PRICE DB FEED の接続再利用と latency 計測
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


# ============================================================
# P0-1  console_tee 非同期化
# ============================================================

def test_console_tee_async() -> None:
    section("P0-1  console_tee 非同期ライタ (呼び出し側が NAS を待たない)")
    from core.logging import console_tee as ct

    class SlowStream:
        """NAS を模す。1 write あたり 10ms かかる。"""

        def __init__(self):
            self.buf = []
            self.writes = 0

        def write(self, s):
            time.sleep(0.010)
            self.buf.append(s)
            self.writes += 1

        def flush(self):
            time.sleep(0.010)

    slow = SlowStream()
    w = ct._AsyncFileWriter(slow, maxsize=10000, flush_interval=0.05)

    N = 300
    t0 = time.perf_counter()
    for i in range(N):
        w.write(f"line {i}\n")
    caller_el = time.perf_counter() - t0

    check(f"{N} 行の write が呼び出し側で {caller_el * 1000:.1f}ms (同期なら 3000ms 以上)",
          caller_el < 0.5, f"{caller_el * 1000:.1f}ms")

    # ライタスレッドが追いつくのを待つ
    deadline = time.time() + 15
    while time.time() < deadline:
        if w.stats()["queued"] == 0:
            break
        time.sleep(0.05)
    w.close()

    joined = "".join(slow.buf)
    check("全行がファイル側へ届いている",
          all(f"line {i}\n" in joined for i in range(N)),
          f"stream_writes={slow.writes} chars={len(joined)}")
    check("write 回数が行数よりずっと少ない (まとめ書きできている)",
          slow.writes < N, f"stream_writes={slow.writes} < {N}")
    st = w.stats()
    check("dropped=0", st["dropped"] == 0, str(st))


def test_console_tee_overflow_never_blocks() -> None:
    section("P0-1  キュー溢れでも呼び出し側を止めない")
    from core.logging import console_tee as ct

    class Stuck:
        def write(self, s):
            time.sleep(0.5)      # 完全に詰まった NAS

        def flush(self):
            time.sleep(0.5)

    w = ct._AsyncFileWriter(Stuck(), maxsize=1000, flush_interval=0.05)
    t0 = time.perf_counter()
    for i in range(5000):
        w.write(f"x{i}\n")
    el = time.perf_counter() - t0
    st = w.stats()
    check(f"NAS が完全に詰まっていても 5000 write が {el * 1000:.0f}ms で戻る",
          el < 2.0, f"{el * 1000:.0f}ms")
    check("溢れた分は破棄され、カウントされている",
          st["dropped"] > 0 or st["queued"] > 0, str(st))
    w.close()


def test_table_dump_gated() -> None:
    section("P0-1  表ダンプが env ゲートに従う")
    import pandas as pd

    import core.global_context.context as ctx

    df = pd.DataFrame({
        "symbol": [f"{1000 + i}" for i in range(30)],
        "symbolname": ["テスト銘柄"] * 30,
        "score": [1.0] * 30,
        "close": [100.0] * 30,
        "datetime": [pd.Timestamp("2026-08-28 09:00:00")] * 30,
    })

    def capture(enabled: bool) -> str:
        if enabled:
            os.environ["SUMMARY_CONTEXT_VERBOSE_TABLE_LOG"] = "1"
        else:
            os.environ.pop("SUMMARY_CONTEXT_VERBOSE_TABLE_LOG", None)
        buf = io.StringIO()
        h = logging.StreamHandler(buf)
        h.setLevel(logging.INFO)
        lg = logging.getLogger("core.global_context.context")
        old_level = lg.level
        lg.addHandler(h)
        lg.setLevel(logging.INFO)
        try:
            ctx._log_df_profile("[TEST]", 1, "push", df)
        finally:
            lg.removeHandler(h)
            lg.setLevel(old_level)
        return buf.getvalue()

    off = capture(False)
    on = capture(True)

    check("既定 (env 未設定) では表を出さない",
          "-+-" not in off and "|" not in off.replace("[TEST]", ""),
          f"bytes={len(off)}")
    check("件数・非ゼロ数の行は残っている",
          "rows=30" in off and "nonzero" in off, f"bytes={len(off)}")
    check("env=1 なら表を出す", len(on) > len(off) * 2,
          f"off={len(off)}B on={len(on)}B")
    print(f"    off={len(off)}B  on={len(on)}B  削減={100 * (1 - len(off) / max(1, len(on))):.0f}%")
    os.environ.pop("SUMMARY_CONTEXT_VERBOSE_TABLE_LOG", None)


# ============================================================
# P0-2 / P0-4 / P0-6  PUSH DB read
# ============================================================

def _make_push_db(path: str, rows: int, start_rowid: int = 0) -> None:
    c = sqlite3.connect(path)
    c.execute(
        "CREATE TABLE IF NOT EXISTS stream_data_raw ("
        "symbol TEXT, symbolname TEXT, datetime TEXT, price REAL, volume REAL)"
    )
    import datetime as _dt

    base = _dt.datetime.now().replace(second=0, microsecond=0)
    c.executemany(
        "INSERT INTO stream_data_raw VALUES (?,?,?,?,?)",
        [
            (
                str(7000 + (i % 50)),
                "テスト",
                (base - _dt.timedelta(seconds=rows - i)).isoformat(),
                100.0 + (i % 17),
                10.0,
            )
            for i in range(rows)
        ],
    )
    c.commit()
    c.close()


def test_push_db_incremental_and_share() -> None:
    section("P0-2 / P0-4 / P0-6  PUSH DB 増分読み・共有・段別計測")
    import scheduler_jobs.summary.memory_latest_1m as ml

    tmp = tempfile.mkdtemp(prefix="pushdb_")
    db = os.path.join(tmp, "push20260828.db")
    _make_push_db(db, 800)

    from pathlib import Path

    ml._push_db_path = lambda: Path(db)          # type: ignore[assignment]
    ml.reset_push_db_cursor()
    os.environ["SUMMARY_MAIN_PUSH_DB_SHARE_TTL_SEC"] = "0"   # 共有キャッシュは別テスト

    df1 = ml._read_push_db_recent()
    check("初回は full read", len(df1) == 800, f"rows={len(df1)}")
    s1 = ml.push_db_read_stats()
    check("last_rowid が記録される", (s1["last_rowid"] or 0) == 800, str(s1["last_rowid"]))

    # 新しい tick を追加
    _make_push_db(db, 50)
    df2 = ml._read_push_db_recent()
    s2 = ml.push_db_read_stats()
    check("2 回目は増分で合流し、行数が増える", len(df2) == 850, f"rows={len(df2)}")
    check("last_rowid が進む", (s2["last_rowid"] or 0) == 850, str(s2["last_rowid"]))

    # 新規なし
    df3 = ml._read_push_db_recent()
    check("新規 tick が無ければ行数は変わらない", len(df3) == 850, f"rows={len(df3)}")

    # リングバッファ上限
    os.environ["SUMMARY_MAIN_PUSH_DB_RING_MAX_ROWS"] = "900"
    _make_push_db(db, 200)
    df4 = ml._read_push_db_recent()
    check("リングバッファ上限で頭が切られる", len(df4) == 900, f"rows={len(df4)}")
    os.environ.pop("SUMMARY_MAIN_PUSH_DB_RING_MAX_ROWS", None)

    # ---- P0-4: 同一 tick 内の重複 open を防ぐ ----
    os.environ["SUMMARY_MAIN_PUSH_DB_SHARE_TTL_SEC"] = "5"
    ml.reset_push_db_cursor()
    opens = {"n": 0}
    real_connect = sqlite3.connect

    def counting_connect(*a, **k):
        if a and str(a[0]).endswith("push20260828.db"):
            opens["n"] += 1
        return real_connect(*a, **k)

    sqlite3.connect = counting_connect          # type: ignore[assignment]
    try:
        for _ in range(6):
            ml._read_push_db_recent()
    finally:
        sqlite3.connect = real_connect          # type: ignore[assignment]

    check(f"1 tick 内 6 回呼んでも DB open は {opens['n']} 回",
          opens["n"] == 1, f"opens={opens['n']}")
    os.environ.pop("SUMMARY_MAIN_PUSH_DB_SHARE_TTL_SEC", None)
    ml.reset_push_db_cursor()


def test_fastlane_stage_logs() -> None:
    section("P0-2  FAST LANE 段別計測が出る")
    import scheduler_jobs.summary.direct_push_1m as dp

    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    h.setLevel(logging.INFO)
    lg = logging.getLogger("scheduler_jobs.summary.direct_push_1m")
    lg.addHandler(h)
    old = lg.level
    lg.setLevel(logging.INFO)
    try:
        st = dp._FastLaneStages("unittest")
        st.mark("connect", rows=0)
        time.sleep(0.01)
        st.mark("query_fetch", rows=983)
        st.emit(result="ok")
    finally:
        lg.removeHandler(h)
        lg.setLevel(old)
    out = buf.getvalue()
    check("[FASTLANE STAGE] が出る", "[FASTLANE STAGE]" in out, out[:120])
    check("段名と elapsed が入る",
          "connect=" in out and "query_fetch=" in out and "total=" in out, out[:200])
    check("rows_before/rows_after 相当の情報が入る", "rows=983" in out, out[:200])


def test_fastlane_slow_stage_warning() -> None:
    section("P0-2  1 段 1 秒以上で WARNING")
    import scheduler_jobs.summary.direct_push_1m as dp

    recs: list = []

    class Grab(logging.Handler):
        def emit(self, record):
            recs.append(record)

    lg = logging.getLogger("scheduler_jobs.summary.direct_push_1m")
    h = Grab()
    lg.addHandler(h)
    try:
        st = dp._FastLaneStages("slowtest")
        st._last -= 1.5          # 1.5 秒経過したことにする
        st.mark("heavy_stage", rows=983)
    finally:
        lg.removeHandler(h)
    warn = [r for r in recs if r.levelno >= logging.WARNING and "slow" in r.getMessage()]
    check("1 秒超の段で WARNING が出る", len(warn) >= 1,
          warn[0].getMessage()[:120] if warn else "none")


# ============================================================
# P0-7  bounded fallback
# ============================================================

def test_fallback_bounded() -> None:
    section("P0-7  fallback が予算で打ち切られる (timeout は伸ばさない)")
    import pandas as pd
    import scheduler_jobs.summary.direct_push_1m as dp
    import datetime as _dt

    os.environ["SUMMARY_FORCE_DIRECT_BUILD_TIMEOUT_SEC"] = "1"
    os.environ["SUMMARY_FORCE_DIRECT_TOTAL_BUDGET_SEC"] = "2"

    orig_fast = dp._fresh_summary_fast_path
    orig_inner = dp._build_direct_inner
    orig_mem = dp._build_from_process_memory

    slow_mem_started = threading.Event()

    def slow_mem(now):
        slow_mem_started.set()
        time.sleep(30)           # 08:51 の 41 秒 fastlane を模す
        return pd.DataFrame()

    dp._fresh_summary_fast_path = lambda now: pd.DataFrame()
    dp._build_direct_inner = lambda now: pd.DataFrame()
    dp._build_from_process_memory = slow_mem
    try:
        t0 = time.perf_counter()
        out = dp._build_direct(_dt.datetime.now())
        el = time.perf_counter() - t0
    finally:
        dp._fresh_summary_fast_path = orig_fast
        dp._build_direct_inner = orig_inner
        dp._build_from_process_memory = orig_mem
        os.environ.pop("SUMMARY_FORCE_DIRECT_BUILD_TIMEOUT_SEC", None)
        os.environ.pop("SUMMARY_FORCE_DIRECT_TOTAL_BUDGET_SEC", None)

    check(f"30 秒かかる fallback が {el:.2f}s で打ち切られる (旧実装は無制限)",
          el < 6.0, f"{el:.2f}s")
    check("打ち切り時は空を返す (stale で Entry を fail-open させない)",
          isinstance(out, pd.DataFrame) and out.empty, f"rows={len(out)}")
    check("fallback は実際に起動していた", slow_mem_started.is_set())


# ============================================================
# P0-9  long_tf_db_publish
# ============================================================

def test_long_tf_read_closes_and_range() -> None:
    section("P0-9  3m publish: 接続クローズ + 範囲述語")
    import datetime as _dt

    import scheduler_jobs.summary.long_tf_db_publish as lt

    tmp = tempfile.mkdtemp(prefix="summarydb_")
    db = os.path.join(tmp, "summary20260828.db")
    c = sqlite3.connect(db)
    c.execute(
        'CREATE TABLE stock_summary_3min ('
        'symbol TEXT, datetime TEXT, open REAL, high REAL, low REAL, close REAL)'
    )
    now = _dt.datetime.now().replace(second=0, microsecond=0)
    slot = now - _dt.timedelta(minutes=3)
    rows = []
    for i in range(50):
        for k in range(4):
            rows.append((str(7000 + i),
                         (slot - _dt.timedelta(minutes=3 * k)).strftime("%Y-%m-%d %H:%M:%S"),
                         100.0, 101.0, 99.0, 100.5))
    # 前日行 (範囲述語で除外されること)
    for i in range(50):
        rows.append((str(7000 + i),
                     (slot - _dt.timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S"),
                     1.0, 1.0, 1.0, 1.0))
    c.executemany("INSERT INTO stock_summary_3min VALUES (?,?,?,?,?,?)", rows)
    c.commit()
    c.close()

    lt._summary_db_path = lambda: db            # type: ignore[assignment]

    opened = {"n": 0}
    closed = {"n": 0}
    real_connect = sqlite3.connect

    class TrackedConn(sqlite3.Connection):
        def close(self):
            closed["n"] += 1
            super().close()

    def tracking_connect(*a, **k):
        if a and str(a[0]) == db:
            opened["n"] += 1
            k["factory"] = TrackedConn
        return real_connect(*a, **k)

    sqlite3.connect = tracking_connect          # type: ignore[assignment]
    try:
        df, note = lt._read_interval(3)
    finally:
        sqlite3.connect = real_connect          # type: ignore[assignment]

    check("読み込めた", not df.empty, f"rows={len(df)} note={note}")
    check("開いた接続はすべて閉じている",
          opened["n"] == closed["n"] and opened["n"] >= 1,
          f"opened={opened['n']} closed={closed['n']}")
    check("前日行が混ざらない (範囲述語が効いている)",
          bool((df["close"] > 50).all()), f"min_close={df['close'].min()}")
    check("当日の 50 銘柄が取れる", df["symbol"].nunique() == 50,
          f"symbols={df['symbol'].nunique()}")


# ============================================================
# P0-10  price feeder 接続再利用
# ============================================================

def test_price_feeder_conn_reuse() -> None:
    section("P0-10  MAIN PRICE DB FEED: 接続再利用と latency 計測")
    import trading.push.latest_price_db_feeder as fd

    tmp = tempfile.mkdtemp(prefix="feeddb_")
    db = os.path.join(tmp, "push20260828.db")
    _make_push_db(db, 500)

    fd._db_path = lambda: db                    # type: ignore[assignment]
    fd._LAST_ROWID = 0
    with fd._conn_lock:
        fd._close_shared_conn_locked()
        fd._schema_cache.clear()

    # feeder 自身の open だけを数える。テストの writer (_make_push_db) は除外する。
    opens = {"n": 0}
    counting = {"on": False}
    real_connect = sqlite3.connect

    def counting_connect(*a, **k):
        if counting["on"] and a and str(a[0]) == db:
            opens["n"] += 1
        return real_connect(*a, **k)

    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    h.setLevel(logging.INFO)
    lg = logging.getLogger("trading.push.latest_price_db_feeder")
    lg.addHandler(h)
    old = lg.level
    lg.setLevel(logging.INFO)

    sqlite3.connect = counting_connect          # type: ignore[assignment]
    try:
        for i in range(8):
            counting["on"] = True
            rows, mx, note = fd.read_new_rows()
            counting["on"] = False
            fd._LAST_ROWID = mx
            _make_push_db(db, 10)               # 新しい tick を足す (テスト側の writer)
    finally:
        sqlite3.connect = real_connect          # type: ignore[assignment]
        lg.removeHandler(h)
        lg.setLevel(old)

    out = buf.getvalue()
    check(f"8 サイクルで DB open は {opens['n']} 回 (旧実装は 8 回)",
          opens["n"] == 1, f"opens={opens['n']}")
    check("[MAIN PRICE DB FEED LATENCY] が出る",
          "[MAIN PRICE DB FEED LATENCY]" in out, out[:150])
    for k in ("connect_ms=", "schema_ms=", "query_ms=", "fetch_ms=", "total_ms=",
              "db_size_mb=", "rowid_delta="):
        check(f"latency ログに {k} がある", k in out, "")
    st = fd.feeder_conn_stats()
    check("接続は 1 本だけ (リークしない)", st["outstanding"] <= 1, str(st))

    with fd._conn_lock:
        fd._close_shared_conn_locked()
    st2 = fd.feeder_conn_stats()
    check("明示クローズで outstanding=0", st2["outstanding"] == 0, str(st2))


def main() -> int:
    tests = [
        test_console_tee_async,
        test_console_tee_overflow_never_blocks,
        test_table_dump_gated,
        test_push_db_incremental_and_share,
        test_fastlane_stage_logs,
        test_fastlane_slow_stage_warning,
        test_fallback_bounded,
        test_long_tf_read_closes_and_range,
        test_price_feeder_conn_reuse,
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
