# -*- coding: utf-8 -*-
"""
第18ラウンド: ranking DB writer の背景 checkpoint (実注文なし)

第16ラウンドで push DB に対して特定した
「inline WAL checkpoint が commit を詰まらせる」構造が
ranking 側にも残っていたので移植した。その検証。
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

PASS = 0
FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  OK   {label} {detail}")
    else:
        FAIL += 1
        print(f"  FAIL {label} {detail}")


import trading.ranking.ranking_db_writer as rw

# ============================================================
print("[1] env の既定値と切り替え")
# ============================================================
for k in ("RANKING_DB_BG_CHECKPOINT", "RANKING_DB_BG_CHECKPOINT_INTERVAL_SEC",
          "RANKING_DB_WAL_SIZE_LIMIT_MB"):
    os.environ.pop(k, None)

check("既定で背景 checkpoint は有効", rw._ranking_bg_checkpoint_enabled() is True)
check("既定 interval = 30 秒", rw._ranking_bg_checkpoint_interval_sec() == 30.0)
check("既定 WAL 上限 = 64MB", rw._ranking_wal_size_limit_bytes() == 64 * 1024 * 1024)

os.environ["RANKING_DB_BG_CHECKPOINT"] = "0"
check("env=0 で無効化できる", rw._ranking_bg_checkpoint_enabled() is False)
os.environ["RANKING_DB_BG_CHECKPOINT"] = "1"
check("env=1 で有効化できる", rw._ranking_bg_checkpoint_enabled() is True)

os.environ["RANKING_DB_BG_CHECKPOINT_INTERVAL_SEC"] = "1"
check("interval は最小 5 秒でクランプ", rw._ranking_bg_checkpoint_interval_sec() == 5.0)
os.environ["RANKING_DB_BG_CHECKPOINT_INTERVAL_SEC"] = "45"
check("interval を指定できる", rw._ranking_bg_checkpoint_interval_sec() == 45.0)
os.environ.pop("RANKING_DB_BG_CHECKPOINT_INTERVAL_SEC", None)

os.environ["RANKING_DB_WAL_SIZE_LIMIT_MB"] = "16"
check("WAL 上限を指定できる", rw._ranking_wal_size_limit_bytes() == 16 * 1024 * 1024)
os.environ.pop("RANKING_DB_WAL_SIZE_LIMIT_MB", None)

# ============================================================
print("[2] inline auto-checkpoint が commit を詰まらせることの実測")
# ============================================================
TMP = tempfile.mkdtemp(prefix="r18_ckpt_")


def bench(autockpt: int, n_commit: int = 120, rows: int = 300):
    db = os.path.join(TMP, "b_%d.db" % autockpt)
    for suf in ("", "-wal", "-shm"):
        try:
            os.remove(db + suf)
        except OSError:
            pass
    conn = sqlite3.connect(db, timeout=30)
    cur = conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL;")
    cur.execute("PRAGMA synchronous=NORMAL;")
    cur.execute("PRAGMA wal_autocheckpoint=%d;" % autockpt)
    cols = ", ".join("c%d TEXT" % i for i in range(40))
    cur.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, %s)" % cols)
    conn.commit()

    payload = ["x" * 60] * 40
    times = []
    for i in range(n_commit):
        cur.executemany(
            "INSERT INTO t VALUES (NULL, %s)" % ", ".join(["?"] * 40),
            [payload for _ in range(rows)],
        )
        t0 = time.perf_counter()
        conn.commit()
        times.append(time.perf_counter() - t0)
    try:
        wal = os.path.getsize(db + "-wal")
    except OSError:
        wal = 0
    conn.close()
    times.sort()
    return {
        "p50": times[len(times) // 2],
        "p95": times[int(len(times) * 0.95)],
        "max": times[-1],
        "wal_mb": wal / 1048576.0,
    }


inline = bench(1000)   # 旧: 約 4MB ごとに commit の中で checkpoint
off = bench(0)         # 新: inline を止める (背景スレッドが後で PASSIVE)

print("    inline (wal_autocheckpoint=1000): p50=%.4fs p95=%.4fs max=%.4fs wal=%.1fMB"
      % (inline["p50"], inline["p95"], inline["max"], inline["wal_mb"]))
print("    off    (wal_autocheckpoint=0)   : p50=%.4fs p95=%.4fs max=%.4fs wal=%.1fMB"
      % (off["p50"], off["p95"], off["max"], off["wal_mb"]))

check("inline を外すと commit の最悪値が改善する", off["max"] <= inline["max"],
      "max %.4fs -> %.4fs" % (inline["max"], off["max"]))
check("inline を外すと commit の p95 が改善する", off["p95"] <= inline["p95"],
      "p95 %.4fs -> %.4fs" % (inline["p95"], off["p95"]))

# ============================================================
print("[3] 背景 checkpoint スレッドが WAL を実際に縮める")
# ============================================================
db = os.path.join(TMP, "bg.db")
conn = sqlite3.connect(db, timeout=30)
cur = conn.cursor()
cur.execute("PRAGMA journal_mode=WAL;")
cur.execute("PRAGMA wal_autocheckpoint=0;")
cur.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
conn.commit()
for i in range(200):
    cur.executemany("INSERT INTO t VALUES (NULL, ?)", [("y" * 500,) for _ in range(200)])
    conn.commit()
wal_before = os.path.getsize(db + "-wal") / 1048576.0

os.environ["RANKING_DB_BG_CHECKPOINT"] = "1"
os.environ["RANKING_DB_BG_CHECKPOINT_INTERVAL_SEC"] = "5"


class _Dummy(rw.RankingDBWriter):
    def __init__(self):      # 接続もスレッドも張らない
        pass


rw.RankingDBWriter._BG_CKPT_THREAD = None
d = _Dummy()
d._start_bg_checkpoint(db)
check("背景スレッドが起動する",
      rw.RankingDBWriter._BG_CKPT_THREAD is not None
      and rw.RankingDBWriter._BG_CKPT_THREAD.is_alive())

t_wait = 0.0
while t_wait < 25.0:
    time.sleep(1.0)
    t_wait += 1.0
    if rw.RankingDBWriter.checkpoint_stats()["runs"] >= 1:
        break

st = rw.RankingDBWriter.checkpoint_stats()
check("PASSIVE checkpoint が実行された", st["runs"] >= 1, str(st))
check("checkpoint でエラーが出ていない", st["errors"] == 0, str(st))

try:
    wal_after = os.path.getsize(db + "-wal") / 1048576.0
except OSError:
    wal_after = 0.0
check("WAL が縮んだ (または切り詰められた)", wal_after <= wal_before,
      "%.1fMB -> %.1fMB" % (wal_before, wal_after))

d2 = _Dummy()
d2._start_bg_checkpoint(db)
check("スレッドは 1 本だけ (二重起動しない)",
      sum(1 for t in __import__("threading").enumerate()
          if t.name == "ranking-db-checkpoint") == 1)

conn.close()
for k in ("RANKING_DB_BG_CHECKPOINT", "RANKING_DB_BG_CHECKPOINT_INTERVAL_SEC"):
    os.environ.pop(k, None)

# ============================================================
print("[4] _open_connection() 経路で PRAGMA が効く")
# ============================================================
# 本番 ranking DB を絶対に触らないよう、パス解決を一時ファイルへ差し替える。
import pathlib

tmp_db = pathlib.Path(TMP) / "open_conn.db"
orig_resolve = rw._resolve_ranking_db_path
rw._resolve_ranking_db_path = lambda: tmp_db

os.environ["RANKING_DB_BG_CHECKPOINT"] = "1"
os.environ["RANKING_DB_WAL_SIZE_LIMIT_MB"] = "32"
rw.RankingDBWriter._BG_CKPT_THREAD = None

w = rw.RankingDBWriter.__new__(rw.RankingDBWriter)
w.buffer_size = 100
w.flush_interval_sec = 1.0
w.enable_legacy_save = False
try:
    w._open_connection()

    autockpt = w.cursor.execute("PRAGMA wal_autocheckpoint").fetchone()[0]
    jsl = w.cursor.execute("PRAGMA journal_size_limit").fetchone()[0]
    jmode = w.cursor.execute("PRAGMA journal_mode").fetchone()[0]

    check("journal_mode=WAL のまま", str(jmode).lower() == "wal", str(jmode))
    check("wal_autocheckpoint=0 (inline を止めた)", int(autockpt) == 0, str(autockpt))
    check("journal_size_limit が設定される", int(jsl) == 32 * 1024 * 1024, str(jsl))
    check("背景 checkpoint スレッドが起動する",
          rw.RankingDBWriter._BG_CKPT_THREAD is not None
          and rw.RankingDBWriter._BG_CKPT_THREAD.is_alive())
    check("本番 DB ではなく一時 DB を開いている",
          str(w.db_path) == str(tmp_db), str(w.db_path))

    # 無効化したときは従来値へ戻る
    os.environ["RANKING_DB_BG_CHECKPOINT"] = "0"
    tmp_db2 = pathlib.Path(TMP) / "open_conn_off.db"
    rw._resolve_ranking_db_path = lambda: tmp_db2
    w2 = rw.RankingDBWriter.__new__(rw.RankingDBWriter)
    w2.buffer_size = 100
    w2.flush_interval_sec = 1.0
    w2.enable_legacy_save = False
    w2._open_connection()
    a2 = w2.cursor.execute("PRAGMA wal_autocheckpoint").fetchone()[0]
    check("env=0 なら従来の wal_autocheckpoint に戻る",
          int(a2) == rw.DEFAULT_WAL_AUTOCHECKPOINT, str(a2))
    try:
        w2.conn.close()
    except Exception:
        pass
finally:
    try:
        w.conn.close()
    except Exception:
        pass
    rw._resolve_ranking_db_path = orig_resolve
    for k in ("RANKING_DB_BG_CHECKPOINT", "RANKING_DB_WAL_SIZE_LIMIT_MB"):
        os.environ.pop(k, None)

print(f"\n結果: {PASS} / {PASS + FAIL}")
sys.exit(1 if FAIL else 0)
