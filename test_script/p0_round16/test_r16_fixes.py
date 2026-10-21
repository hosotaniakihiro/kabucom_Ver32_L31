"""第16ラウンド dry-run テスト.

実注文なし。本番 DB は触らない (NAS 上に一時 DB を作る)。

対象:
  P0-1  PUSH DB writer: WAL checkpoint を commit 経路から外す
"""
from __future__ import annotations

import datetime as dt
import io
import logging
import os
import sqlite3
import statistics
import sys
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
    print("=" * 74)
    print(t)
    print("=" * 74)


def test_checkpoint_config() -> None:
    section("P0-1  checkpoint 設定ヘルパ")
    import trading.push.push_db_writer as w

    check("既定で background checkpoint 有効", w._push_bg_checkpoint_enabled() is True)
    os.environ["PUSH_DB_BG_CHECKPOINT"] = "0"
    try:
        check("PUSH_DB_BG_CHECKPOINT=0 で無効化できる",
              w._push_bg_checkpoint_enabled() is False)
    finally:
        os.environ.pop("PUSH_DB_BG_CHECKPOINT", None)

    check("checkpoint 間隔の既定は 30 秒",
          abs(w._push_bg_checkpoint_interval_sec() - 30.0) < 0.01,
          str(w._push_bg_checkpoint_interval_sec()))
    os.environ["PUSH_DB_BG_CHECKPOINT_INTERVAL_SEC"] = "5"
    try:
        check("間隔を env で変えられる",
              abs(w._push_bg_checkpoint_interval_sec() - 5.0) < 0.01)
    finally:
        os.environ.pop("PUSH_DB_BG_CHECKPOINT_INTERVAL_SEC", None)

    check("WAL サイズ上限の既定は 64MB",
          w._push_wal_size_limit_bytes() == 64 * 1024 * 1024,
          str(w._push_wal_size_limit_bytes()))

    src = io.open(os.path.join(ROOT, "trading", "push", "push_db_writer.py"),
                  encoding="utf-8").read()
    check("有効時は wal_autocheckpoint=0 にする",
          "PRAGMA wal_autocheckpoint=0;" in src)
    check("無効時は従来の 1000 に戻す",
          "PRAGMA wal_autocheckpoint=1000;" in src)
    check("journal_size_limit を設定する", "journal_size_limit" in src)
    check("_start_bg_checkpoint を connect で呼ぶ",
          "self._start_bg_checkpoint(db_path)" in src)
    check("PASSIVE checkpoint を使う (writer/reader をブロックしない)",
          "wal_checkpoint(PASSIVE)" in src)
    check("checkpoint_stats() で観測できる", hasattr(w.StreamDBWriter, "checkpoint_stats"))


def test_checkpoint_thread_singleton() -> None:
    section("P0-1  checkpoint スレッドはプロセスで 1 本だけ")
    import threading

    import trading.push.push_db_writer as w

    os.environ["PUSH_DB_BG_CHECKPOINT_INTERVAL_SEC"] = "3600"  # 実行はさせない
    base = os.path.join("X:" + os.sep, "raw_data", "kabu_station", "push")
    db = os.path.join(base, "_r16_ckpt_singleton.db")
    for s in ("", "-wal", "-shm"):
        try:
            os.remove(db + s)
        except FileNotFoundError:
            pass
    sqlite3.connect(db).close()

    cls = w.StreamDBWriter
    with cls._BG_CKPT_LOCK:
        cls._BG_CKPT_THREAD = None
    before = [t.name for t in threading.enumerate() if t.name == "push-db-checkpoint"]
    try:
        inst = cls.__new__(cls)
        for _ in range(5):
            inst._start_bg_checkpoint(db)
        names = [t.name for t in threading.enumerate() if t.name == "push-db-checkpoint"]
        check(f"5 回呼んでも checkpoint スレッドは {len(names)} 本",
              len(names) <= 1, f"threads={names} before={before}")
    finally:
        os.environ.pop("PUSH_DB_BG_CHECKPOINT_INTERVAL_SEC", None)
        for s in ("", "-wal", "-shm"):
            try:
                os.remove(db + s)
            except FileNotFoundError:
                pass

    os.environ["PUSH_DB_BG_CHECKPOINT"] = "0"
    try:
        with cls._BG_CKPT_LOCK:
            cls._BG_CKPT_THREAD = None
        inst2 = cls.__new__(cls)
        inst2._start_bg_checkpoint(db)
        with cls._BG_CKPT_LOCK:
            started = cls._BG_CKPT_THREAD
        check("無効時はスレッドを作らない", started is None, str(started))
    finally:
        os.environ.pop("PUSH_DB_BG_CHECKPOINT", None)


def test_inline_checkpoint_is_the_cost() -> None:
    section("P0-1  inline auto-checkpoint が commit を遅くすることの再現")
    base = os.path.join("X:" + os.sep, "raw_data", "kabu_station", "push")
    ncols = 4 + 40

    def run(autockpt: int) -> tuple:
        p = os.path.join(base, f"_r16_ckpt_{autockpt}.db")
        for s in ("", "-wal", "-shm"):
            try:
                os.remove(p + s)
            except FileNotFoundError:
                pass
        c = sqlite3.connect(p, timeout=30)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute(f"PRAGMA wal_autocheckpoint={autockpt}")
        c.execute("PRAGMA busy_timeout=30000")
        c.execute("CREATE TABLE t (symbol TEXT, datetime TEXT, price REAL, volume REAL, "
                  + ",".join(f"c{i} TEXT" for i in range(40)) + ")")
        c.commit()
        commits = []
        for _ in range(120):
            rows = [tuple([str(1000 + i % 200), dt.datetime.now().isoformat(), 100.0, 1.0]
                          + ["x" * 40] * 40) for i in range(29)]
            c.executemany(f"INSERT INTO t VALUES ({','.join('?' * ncols)})", rows)
            t0 = time.perf_counter()
            c.commit()
            commits.append(time.perf_counter() - t0)
        c.close()
        for s in ("", "-wal", "-shm"):
            try:
                os.remove(p + s)
            except FileNotFoundError:
                pass
        cs = sorted(commits)
        return statistics.mean(cs), cs[-1], sum(cs)

    m_on, max_on, tot_on = run(1000)
    m_off, max_off, tot_off = run(0)
    print(f"    inline autocheckpoint : mean={m_on * 1000:7.1f}ms max={max_on * 1000:8.1f}ms total={tot_on:.2f}s")
    print(f"    autocheckpoint OFF    : mean={m_off * 1000:7.1f}ms max={max_off * 1000:8.1f}ms total={tot_off:.2f}s")
    check("autocheckpoint OFF のほうが commit 平均が速い", m_off <= m_on,
          f"{m_on * 1000:.1f}ms -> {m_off * 1000:.1f}ms")
    check("commit の最悪値が下がる (inline checkpoint のスパイクが消える)",
          max_off <= max_on, f"{max_on * 1000:.1f}ms -> {max_off * 1000:.1f}ms")


def test_passive_checkpoint_is_nonblocking() -> None:
    section("P0-1  PASSIVE checkpoint は writer をブロックしない")
    base = os.path.join("X:" + os.sep, "raw_data", "kabu_station", "push")
    p = os.path.join(base, "_r16_passive.db")
    for s in ("", "-wal", "-shm"):
        try:
            os.remove(p + s)
        except FileNotFoundError:
            pass
    w = sqlite3.connect(p, timeout=30)
    w.execute("PRAGMA journal_mode=WAL")
    w.execute("PRAGMA synchronous=NORMAL")
    w.execute("PRAGMA wal_autocheckpoint=0")
    w.execute("CREATE TABLE t (a TEXT, b TEXT)")
    w.commit()
    for _ in range(50):
        w.executemany("INSERT INTO t VALUES (?,?)", [("x" * 100, "y" * 100)] * 200)
        w.commit()

    ck = sqlite3.connect(p, timeout=30)
    ck.execute("PRAGMA busy_timeout=20000")
    row = ck.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
    ck.close()
    check("PASSIVE checkpoint が busy にならず流せる",
          row is not None and not row[0], f"result={row}")

    # checkpoint 後も writer は書ける
    t0 = time.perf_counter()
    w.executemany("INSERT INTO t VALUES (?,?)", [("z", "z")] * 100)
    w.commit()
    el = time.perf_counter() - t0
    check(f"checkpoint 後も writer は書ける ({el * 1000:.1f}ms)", el < 5.0, f"{el * 1000:.1f}ms")
    w.close()
    for s in ("", "-wal", "-shm"):
        try:
            os.remove(p + s)
        except FileNotFoundError:
            pass


def main() -> int:
    tests = [
        test_checkpoint_config,
        test_checkpoint_thread_singleton,
        test_inline_checkpoint_is_the_cost,
        test_passive_checkpoint_is_nonblocking,
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
