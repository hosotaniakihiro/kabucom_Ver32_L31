"""Win32 ハンドル数で sqlite 接続リークを実測する。

`with sqlite3.connect(...) as conn:` は接続を閉じない (Python 公式仕様)。
runtime_state_store.py の全関数がこの形で close() が無い。
NAS(SMB) 上の DB では 1 接続 = SMB のファイルオープンなので、
これが積み上がるとサーバ側のオープン上限に当たり、
新規オープンが ERROR_TOO_MANY_OPEN_FILES / SQLITE_CANTOPEN /
SQLITE_IOERR(disk I/O error) で失敗する。
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import gc
import os
import sqlite3
import tempfile

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.GetProcessHandleCount.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
_k32.GetProcessHandleCount.restype = wintypes.BOOL
_k32.GetCurrentProcess.restype = wintypes.HANDLE


def handles() -> int:
    n = wintypes.DWORD(0)
    if _k32.GetProcessHandleCount(_k32.GetCurrentProcess(), ctypes.byref(n)):
        return int(n.value)
    return -1


def main() -> None:
    d = tempfile.mkdtemp(prefix="hleak_")
    path = os.path.join(d, "runtime_state.db")
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE IF NOT EXISTS t(a INTEGER)")
    c.commit()
    c.close()
    gc.collect()

    base = handles()
    print(f"baseline handles                    : {base}")

    # --- 本番と同じ形: with のみ、close 無し、conn 参照が生き続ける ---
    live = []

    def production_style(i: int) -> None:
        with sqlite3.connect(path, timeout=30) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("INSERT INTO t VALUES (?)", (i,))
            conn.commit()
        live.append(conn)          # cursor/例外/呼び出し元が掴む状況の再現

    for i in range(500):
        production_style(i)
    h1 = handles()
    print(f"after 500 x with-only (refs held)   : {h1}  (+{h1 - base})")

    live.clear()
    gc.collect()
    h2 = handles()
    print(f"after drop refs + gc.collect()      : {h2}  (+{h2 - base})")

    # --- 修正後の形: 明示 close ---
    for i in range(500):
        conn = sqlite3.connect(path, timeout=30)
        try:
            conn.execute("INSERT INTO t VALUES (?)", (i,))
            conn.commit()
        finally:
            conn.close()
    h3 = handles()
    print(f"after 500 x explicit close()        : {h3}  (+{h3 - base})")

    print()
    print("結論:")
    print(f"  with のみ (参照保持) -> ハンドル +{h1 - base}")
    print(f"  明示 close()         -> ハンドル +{h3 - base}")


if __name__ == "__main__":
    main()
