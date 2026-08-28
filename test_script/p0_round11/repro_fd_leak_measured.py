"""runtime_state_store.py の `with sqlite3.connect(...)` が
プロセスのハンドル/fd を食い潰すことを実測する。

psutil が無くても Windows API (GetProcessHandleCount) で測る。
"""
from __future__ import annotations

import ctypes
import gc
import os
import sqlite3
import sys
import tempfile


def handle_count() -> int:
    try:
        n = ctypes.c_ulong(0)
        h = ctypes.windll.kernel32.GetCurrentProcess()
        if ctypes.windll.kernel32.GetProcessHandleCount(h, ctypes.byref(n)):
            return int(n.value)
    except Exception:
        pass
    return -1


def max_stdio() -> int:
    try:
        return int(ctypes.cdll.msvcrt._getmaxstdio())
    except Exception:
        return -1


def crt_fds_in_use(path: str) -> int:
    """CRT fd を実際に消費して残り本数を測る (open() ベース)。"""
    fds = []
    try:
        while True:
            fds.append(open(path, "rb"))
    except OSError:
        pass
    n = len(fds)
    for f in fds:
        f.close()
    return n


def main() -> None:
    d = tempfile.mkdtemp(prefix="fdleak_")
    path = os.path.join(d, "runtime_state.db")
    probe = os.path.join(d, "probe.txt")
    open(probe, "w").write("x")
    sqlite3.connect(path).close()

    print("maxstdio           :", max_stdio())
    print("free CRT fds (base):", crt_fds_in_use(probe))
    print("handles      (base):", handle_count())

    # 現行 runtime_state_store と同じ形をそのまま再現する。
    # 呼び出し側は返り値を捨てるが、conn は関数ローカルに残り続ける。
    def save_like_production(i: int) -> None:
        with sqlite3.connect(path, timeout=30) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("CREATE TABLE IF NOT EXISTS t(a INTEGER)")
            conn.execute("INSERT INTO t VALUES (?)", (i,))
            conn.commit()
        # ここで close() が無い = 本番と同じ

    leaked = []

    def save_like_production_with_live_ref(i: int) -> None:
        """例外オブジェクト / cursor / traceback が conn を掴んだ場合。
        本番では except 節や logger.exception が traceback を保持しうる。"""
        conn = sqlite3.connect(path, timeout=30)
        with conn:
            cur = conn.execute("INSERT INTO t VALUES (?)", (i,))
        leaked.append(cur)   # cursor が conn を参照し続ける

    for i in range(400):
        save_like_production(i)
    gc.collect()
    print()
    print("after 400x with-only (no lingering ref):")
    print("  free CRT fds:", crt_fds_in_use(probe), " handles:", handle_count())

    for i in range(400):
        save_like_production_with_live_ref(i)
    print()
    print("after 400x with-only (cursor kept alive):")
    print("  free CRT fds:", crt_fds_in_use(probe), " handles:", handle_count())

    leaked.clear()
    gc.collect()
    print()
    print("after releasing refs + gc:")
    print("  free CRT fds:", crt_fds_in_use(probe), " handles:", handle_count())


if __name__ == "__main__":
    main()
