"""SMB ハンドル圧迫下での positions.db disk I/O error 再現.

仮説: 本番の disk I/O error は WAL/SMB そのものではなく、
プロセスが NAS 上のファイルハンドル (fd) を大量に保持した状態で
新規接続すると SQLite の open/lock が失敗する、というもの。

NAS 上に一時ファイルを N 個 open したまま positions.db を叩く。
実注文は一切行わない。
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
import threading
import time
from collections import Counter

BASE = os.environ.get("REPRO_BASE_DIR", os.path.join("X:" + os.sep, "trading", "runtime", "positions"))
SRC = os.path.join(BASE, "positions.db")


def _hold_handles(n: int, holder_dir: str, opened: list) -> int:
    os.makedirs(holder_dir, exist_ok=True)
    fail_at = -1
    for i in range(n):
        p = os.path.join(holder_dir, f"h{i:05d}.tmp")
        try:
            f = open(p, "w+b")
            f.write(b"x" * 64)
            f.flush()
            opened.append((f, p))
        except Exception as e:  # noqa: BLE001
            fail_at = i
            print(f"  handle open failed at i={i}: {type(e).__name__} {e}")
            break
    return fail_at


def _db_burst(path: str, rounds: int, results: Counter) -> None:
    for _ in range(rounds):
        conn = None
        try:
            conn = sqlite3.connect(path, timeout=10, check_same_thread=False)
            cur = conn.cursor()
            cur.execute("PRAGMA busy_timeout=60000;")
            cur.execute("PRAGMA temp_store=MEMORY;")
            cur.execute("PRAGMA synchronous=NORMAL;")
            cur.execute("PRAGMA foreign_keys=ON;")
            cur.execute("SELECT id FROM positions WHERE status='OPEN'").fetchall()
            cur.close()
            results["ok"] += 1
        except Exception as e:  # noqa: BLE001
            results["err"] += 1
            results[f"{type(e).__name__}:{str(e)[:50]}"] += 1
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--steps", default="0,200,500,1000,2000,4000")
    ap.add_argument("--rounds", type=int, default=60)
    ap.add_argument("--threads", type=int, default=6)
    a = ap.parse_args()

    dst = os.path.join(BASE, "_repro_hp.db")
    for s in ("", "-wal", "-shm", "-journal"):
        try:
            os.remove(dst + s)
        except FileNotFoundError:
            pass
    shutil.copyfile(SRC, dst)

    holder_dir = os.path.join(BASE, "_repro_handles")
    opened: list = []
    prev = 0
    try:
        for step in [int(x) for x in a.steps.split(",")]:
            add = step - prev
            if add > 0:
                fail_at = _hold_handles(add, holder_dir, opened)
                if fail_at >= 0:
                    print(f"held={len(opened)} (could not reach {step})")
            prev = step
            results: Counter = Counter()
            ts = [threading.Thread(target=_db_burst, args=(dst, a.rounds, results), daemon=True)
                  for _ in range(a.threads)]
            t0 = time.perf_counter()
            for t in ts:
                t.start()
            for t in ts:
                t.join(120)
            el = time.perf_counter() - t0
            kinds = {k: v for k, v in results.items() if k not in ("ok", "err")}
            print(f"held_handles={len(opened):5d}  ok={results['ok']:5d} err={results['err']:5d} "
                  f"elapsed={el:6.2f}s  {kinds}")
            sys.stdout.flush()
    finally:
        for f, p in opened:
            try:
                f.close()
            except Exception:
                pass
            try:
                os.remove(p)
            except Exception:
                pass
        try:
            os.rmdir(holder_dir)
        except Exception:
            pass
        for s in ("", "-wal", "-shm", "-journal"):
            try:
                os.remove(dst + s)
            except FileNotFoundError:
                pass


if __name__ == "__main__":
    main()
