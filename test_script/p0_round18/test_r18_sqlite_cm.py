# -*- coding: utf-8 -*-
"""
第18ラウンド: committing_connect の意味検証 (実注文なし)

`with sqlite3.connect(...) as conn:` からの置換なので、
  - 正常終了で commit されること (これが崩れると書き込みが黙って消える)
  - 例外で rollback されること
  - どちらでも close されること (ハンドル枯渇の真因)
を旧挙動と突き合わせて確認する。
"""
from __future__ import annotations

import gc
import os
import sqlite3
import sys
import tempfile
import threading

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from sqlite_cm import committing_connect, reset_stats, stats

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


TMP = tempfile.mkdtemp(prefix="r18_sqlite_cm_")


def newdb(name):
    p = os.path.join(TMP, name)
    conn = sqlite3.connect(p)
    conn.execute("CREATE TABLE t (v INTEGER)")
    conn.commit()
    conn.close()
    return p


def rows(p):
    conn = sqlite3.connect(p)
    try:
        return conn.execute("SELECT COUNT(*) FROM t").fetchone()[0]
    finally:
        conn.close()


# ============================================================
print("[1] commit / rollback が従来の with と一致する")
# ============================================================
# 正常終了 -> commit
a_old, a_new = newdb("old_ok.db"), newdb("new_ok.db")
with sqlite3.connect(a_old) as c:
    c.execute("INSERT INTO t VALUES (1)")
c.close()
with committing_connect(a_new) as c:
    c.execute("INSERT INTO t VALUES (1)")
check("正常終了で commit される", rows(a_old) == rows(a_new) == 1,
      f"旧={rows(a_old)} 新={rows(a_new)}")

# 例外 -> rollback
b_old, b_new = newdb("old_ng.db"), newdb("new_ng.db")
try:
    with sqlite3.connect(b_old) as c:
        c.execute("INSERT INTO t VALUES (2)")
        raise RuntimeError("boom")
except RuntimeError:
    pass
c.close()
try:
    with committing_connect(b_new) as c:
        c.execute("INSERT INTO t VALUES (2)")
        raise RuntimeError("boom")
except RuntimeError:
    pass
check("例外で rollback される", rows(b_old) == rows(b_new) == 0,
      f"旧={rows(b_old)} 新={rows(b_new)}")

def _propagates():
    d = newdb("raise.db")
    try:
        with committing_connect(d) as c:
            c.execute("INSERT INTO t VALUES (3)")
            raise ValueError("x")
    except ValueError:
        return True
    return False


check("例外は握り潰さず伝播する", _propagates())


# ============================================================
print("[2] 必ず close される (ハンドル枯渇の真因)")
# ============================================================
d = newdb("close.db")

def _is_closed(conn):
    try:
        conn.execute("SELECT 1")
        return False
    except sqlite3.ProgrammingError:
        return True
    except Exception:
        return False


held = {}
with committing_connect(d) as c:
    held["conn"] = c
check("with を抜けた接続は使用不可 (close 済み)", _is_closed(held["conn"]))

# 例外経路でも close
held2 = {}
try:
    with committing_connect(d) as c:
        held2["conn"] = c
        raise RuntimeError("boom")
except RuntimeError:
    pass
check("例外経路でも close される", _is_closed(held2["conn"]))

# 旧挙動 (対照): with sqlite3.connect は閉じない
old_conn = None
with sqlite3.connect(d) as c:
    old_conn = c
check("対照: 旧 with sqlite3.connect は閉じていない", not _is_closed(old_conn),
      "(これがハンドル枯渇の原因)")
old_conn.close()

# ============================================================
print("[3] opened / closed が釣り合う")
# ============================================================
reset_stats()
N = 200
for i in range(N):
    with committing_connect(d) as c:
        c.execute("SELECT COUNT(*) FROM t").fetchone()
for i in range(20):
    try:
        with committing_connect(d) as c:
            raise RuntimeError("boom")
    except RuntimeError:
        pass
s = stats()
check("opened == closed", s["opened"] == s["closed"] == N + 20, str(s))
check("outstanding == 0", s["outstanding"] == 0, str(s))
check("close_failed == 0", s["close_failed"] == 0, str(s))

# ============================================================
print("[4] 引数がそのまま sqlite3.connect へ渡る")
# ============================================================
uri = "file:%s?mode=ro" % d.replace("\\", "/")
with committing_connect(uri, uri=True, timeout=5) as c:
    n = c.execute("SELECT COUNT(*) FROM t").fetchone()[0]
check("uri / timeout kwargs が通る", isinstance(n, int), f"rows={n}")

with committing_connect(d, timeout=3.0, detect_types=sqlite3.PARSE_DECLTYPES) as c:
    c.execute("SELECT 1")
check("detect_types など任意 kwargs が通る", True)

ok = {"v": False}


def worker():
    try:
        with committing_connect(d, timeout=10) as c:
            c.execute("SELECT COUNT(*) FROM t").fetchone()
        ok["v"] = True
    except Exception:
        ok["v"] = False


t = threading.Thread(target=worker)
t.start(); t.join(15)
check("別スレッドから使える", ok["v"])

# ============================================================
print("[5] 置換後のモジュールが import できる")
# ============================================================
mods = [
    "trading.ranking.summary.incremental_cache",
    "trading.ranking.summary.candidates",
    "trading.entry.summary_ai.emp_prob_gate",
    "trading.entry.volume_direction_guard",
    "trading.summary.mtf_indicator_fill",
    "trading.audit_logging.recorder",
    "trading.push.subscription_manager.filters",
    "trading.ranking.active_symbols.recent_liquidity",
    "database.sqlite.wal",
    "scheduler_jobs.summary.fallback_loader",
    "ats.ats_ranking.normalizer",
]
import importlib
for m in mods:
    try:
        importlib.import_module(m)
        check(f"import {m}", True)
    except Exception as e:
        check(f"import {m}", False, f"{type(e).__name__}: {e}")

print(f"\n結果: {PASS} / {PASS + FAIL}")
sys.exit(1 if FAIL else 0)
