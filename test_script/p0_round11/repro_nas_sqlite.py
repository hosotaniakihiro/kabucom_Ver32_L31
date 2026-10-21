"""NAS(SMB) SQLite disk I/O error 再現ハーネス.

本番と同じ条件 (UNC パス / NullPool / スレッドごとに毎回新規接続 /
接続ごとに PRAGMA synchronous) で positions.db を叩き、
journal_mode ごとに disk I/O error の発生率を測る。

実注文は一切行わない。読み取り専用のコピー DB を使う。
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
import threading
import time
import traceback
from collections import Counter

UNC_DIR = os.environ.get(
    "REPRO_POSITIONS_DIR",
    os.path.join("X:" + os.sep, "trading", "runtime", "positions"),
)
SRC = os.path.join(UNC_DIR, "positions.db")


def _prepare(mode: str, tag: str) -> str:
    dst = os.path.join(UNC_DIR, f"_repro_{tag}.db")
    for suffix in ("", "-wal", "-shm", "-journal"):
        try:
            os.remove(dst + suffix)
        except FileNotFoundError:
            pass
    shutil.copyfile(SRC, dst)
    conn = sqlite3.connect(dst, timeout=60)
    try:
        got = conn.execute(f"PRAGMA journal_mode={mode};").fetchone()[0]
        conn.commit()
    finally:
        conn.close()
    return dst, got


def _worker(path: str, stop: threading.Event, results: Counter, lock: threading.Lock,
            errors: list, do_write: bool, name: str) -> None:
    while not stop.is_set():
        conn = None
        try:
            # 本番と同じ: NullPool 相当 = クエリごとに新規接続
            conn = sqlite3.connect(path, timeout=60, check_same_thread=False)
            cur = conn.cursor()
            cur.execute("PRAGMA busy_timeout=60000;")
            cur.execute("PRAGMA temp_store=MEMORY;")
            cur.execute("PRAGMA synchronous=NORMAL;")   # ← 本番で落ちている行
            cur.execute("PRAGMA foreign_keys=ON;")
            cur.execute("SELECT id, symbol, status, qty FROM positions WHERE status='OPEN'")
            cur.fetchall()
            cur.execute("SELECT id, symbol, status FROM positions WHERE status='CLOSING'")
            cur.fetchall()
            if do_write:
                cur.execute("UPDATE positions SET updated_at=updated_at WHERE id=(SELECT MIN(id) FROM positions)")
                conn.commit()
            cur.close()
            with lock:
                results["ok"] += 1
        except Exception as e:  # noqa: BLE001
            with lock:
                results["err"] += 1
                results[type(e).__name__ + ":" + str(e)[:60]] += 1
                if len(errors) < 5:
                    errors.append(traceback.format_exc(limit=3))
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass


def run(mode: str, threads: int, seconds: float, writers: int) -> dict:
    tag = mode.lower()
    path, actual = _prepare(mode, tag)
    stop = threading.Event()
    results: Counter = Counter()
    lock = threading.Lock()
    errors: list = []
    ts = []
    for i in range(threads):
        t = threading.Thread(
            target=_worker,
            args=(path, stop, results, lock, errors, i < writers, f"w{i}"),
            daemon=True, name=f"repro-{tag}-{i}",
        )
        t.start()
        ts.append(t)
    t0 = time.perf_counter()
    time.sleep(seconds)
    stop.set()
    for t in ts:
        t.join(10)
    el = time.perf_counter() - t0

    ok = results["ok"]
    err = results["err"]
    out = {
        "requested_mode": mode,
        "actual_mode": actual,
        "threads": threads,
        "writers": writers,
        "elapsed": round(el, 2),
        "ok": ok,
        "err": err,
        "err_rate": round(err / max(1, ok + err), 4),
        "qps": round((ok + err) / el, 1),
        "err_kinds": {k: v for k, v in results.items() if k not in ("ok", "err")},
    }
    for suffix in ("", "-wal", "-shm", "-journal"):
        try:
            os.remove(path + suffix)
        except FileNotFoundError:
            pass
    if errors:
        out["sample_traceback"] = errors[0]
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--threads", type=int, default=8)
    ap.add_argument("--writers", type=int, default=2)
    ap.add_argument("--seconds", type=float, default=20.0)
    ap.add_argument("--modes", default="WAL,TRUNCATE,DELETE")
    a = ap.parse_args()
    for mode in [m.strip() for m in a.modes.split(",") if m.strip()]:
        r = run(mode, a.threads, a.seconds, a.writers)
        tb = r.pop("sample_traceback", None)
        print("-" * 70)
        for k, v in r.items():
            print(f"  {k:16s} {v}")
        if tb:
            print("  sample:")
            for line in tb.strip().splitlines()[-4:]:
                print("    " + line)
        sys.stdout.flush()
