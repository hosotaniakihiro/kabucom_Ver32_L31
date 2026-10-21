# -*- coding: utf-8 -*-
"""第18ラウンド: 置換前後の Win32 ハンドル増加を実測する (第11ラウンドの再現ハーネス相当)。"""
from __future__ import annotations
import os, sqlite3, sys, tempfile, gc
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
import psutil
from sqlite_cm import committing_connect

TMP = tempfile.mkdtemp(prefix="r18_handles_")
DB = os.path.join(TMP, "h.db")
c = sqlite3.connect(DB); c.execute("CREATE TABLE t (v INTEGER)"); c.commit(); c.close()

P = psutil.Process()
N = 500

def handles():
    gc.collect()
    try:
        return P.num_handles()
    except Exception:
        return P.num_fds()

def run_old():
    keep = []
    for _ in range(N):
        with sqlite3.connect(DB) as conn:      # 旧: close しない
            conn.execute("INSERT INTO t VALUES (1)")
        keep.append(conn)                       # traceback/frame が掴む状況を再現
    return keep

def run_new():
    keep = []
    for _ in range(N):
        with committing_connect(DB) as conn:    # 新: 必ず close
            conn.execute("INSERT INTO t VALUES (1)")
        keep.append(conn)
    return keep

base = handles()
kept_old = run_old()
after_old = handles()
d_old = after_old - base

for k in kept_old: 
    try: k.close()
    except Exception: pass
del kept_old
gc.collect()

base2 = handles()
kept_new = run_new()
after_new = handles()
d_new = after_new - base2
del kept_new
gc.collect()

print("接続数            : %d" % N)
print("旧 with sqlite3.connect : ハンドル %+d (1 接続あたり %+.2f)" % (d_old, d_old / N))
print("新 committing_connect   : ハンドル %+d (1 接続あたり %+.2f)" % (d_new, d_new / N))
ok = d_new <= 10 and d_old > d_new
print("判定:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
