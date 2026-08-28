"""`with sqlite3.connect(...) as conn:` が接続を閉じないことの実測.

runtime_state_store.py の全関数がこの形。close() が無い。
Python の `with sqlite3.Connection` はトランザクションを commit/rollback
するだけで、接続そのものは閉じない (公式ドキュメント記載の仕様)。
"""
from __future__ import annotations

import gc
import os
import sqlite3
import sys
import tempfile

try:
    import psutil
except Exception:
    psutil = None


def _handles(p):
    if p is None:
        return None
    try:
        return p.num_handles()
    except Exception:
        return None


def _open_files(p):
    if p is None:
        return None
    try:
        return len(p.open_files())
    except Exception:
        return None


def main() -> None:
    proc = psutil.Process() if psutil else None
    d = tempfile.mkdtemp(prefix="connleak_")
    path = os.path.join(d, "runtime_state.db")

    sqlite3.connect(path).close()

    print("psutil available:", psutil is not None)
    print(f"{'phase':28s} {'handles':>9s} {'open_files':>11s}")

    def snap(label):
        print(f"{label:28s} {str(_handles(proc)):>9s} {str(_open_files(proc)):>11s}")

    snap("baseline")

    # --- 現行実装と同じ形 (close なし) ---
    kept = []
    for i in range(300):
        with sqlite3.connect(path, timeout=30) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("CREATE TABLE IF NOT EXISTS t(a INTEGER)")
            conn.execute("INSERT INTO t VALUES (?)", (i,))
            conn.commit()
        # 例外テーブルや traceback に conn が残ると GC まで生き残る。
        # 現行コードと同じく参照を落とさない状況を再現する。
        kept.append(conn)
    snap("with-only x300 (refs kept)")

    del kept
    gc.collect()
    snap("after del+gc")

    # --- close あり ---
    for i in range(300):
        conn = sqlite3.connect(path, timeout=30)
        try:
            conn.execute("INSERT INTO t VALUES (?)", (i,))
            conn.commit()
        finally:
            conn.close()
    snap("explicit close x300")

    # --- with-only, 参照を残さない (通常の refcount 解放) ---
    for i in range(300):
        with sqlite3.connect(path, timeout=30) as c2:
            c2.execute("INSERT INTO t VALUES (?)", (i,))
        del c2
    gc.collect()
    snap("with-only x300 (no refs)")

    # 接続が本当に開いたままか直接確認
    c = sqlite3.connect(path)
    with c:
        c.execute("INSERT INTO t VALUES (1)")
    try:
        c.execute("SELECT 1")
        print("\n=> with ブロックを抜けた後も接続は生きている (close されていない): True")
    except sqlite3.ProgrammingError as e:
        print("\n=> closed:", e)
    c.close()


if __name__ == "__main__":
    main()
