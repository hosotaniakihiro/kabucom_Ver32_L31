# -*- coding: utf-8 -*-
"""
第18ラウンド dry-run 検証 (実注文なし)

対象:
  1. incremental_cache._trim  … narrow 位置決めが従来経路と同一結果か
  2. incremental_cache._frame_mb … サンプル外挿の誤差と速度
  3. incremental_cache.stats() … _LOCK を保持したまま重い集計をしないこと
"""
from __future__ import annotations

import os
import sys
import threading
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import numpy as np
import pandas as pd

import trading.ranking.summary.incremental_cache as inc

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


def trim_reference(df, bars):
    """R17 までの実装 (期待値)。"""
    out = df.sort_values(["symbol", "datetime"], kind="stable")
    out = out.drop_duplicates(subset=["symbol", "datetime"], keep="last")
    out = out.groupby("symbol", sort=False, group_keys=False).tail(bars)
    return out.sort_values("datetime", kind="stable").reset_index(drop=True)


def make_frame(n_sym, n_bars, n_extra_cols=0, dup_ratio=0.0, seed=0):
    rng = np.random.default_rng(seed)
    syms = [f"{1000+i}" for i in range(n_sym)]
    t0 = pd.Timestamp("2026-08-28 09:00:00")
    rows = {
        "symbol": np.repeat(syms, n_bars),
        "datetime": np.tile([t0 + pd.Timedelta(minutes=b) for b in range(n_bars)], n_sym),
        "close": rng.random(n_sym * n_bars) * 1000.0,
        "tag": rng.choice(["a", "bb", "ccc"], n_sym * n_bars),
    }
    df = pd.DataFrame(rows)
    for c in range(n_extra_cols):
        df[f"x{c}"] = rng.choice(["p", "qq", None], len(df))
    if dup_ratio > 0:
        k = int(len(df) * dup_ratio)
        dup = df.iloc[rng.choice(len(df), size=k, replace=False)].copy()
        dup["close"] = dup["close"] + 1.0     # keep="last" が効くよう値を変える
        df = pd.concat([df, dup], ignore_index=True)
    return df


# ============================================================
print("[1] _trim: 従来経路と同一結果")
# ============================================================
cases = [
    ("小 / 重複なし",        make_frame(5, 10, 0, 0.0, 1), 5),
    ("重複あり",             make_frame(20, 30, 3, 0.25, 2), 10),
    ("bars > 実データ",      make_frame(10, 5, 0, 0.0, 3), 105),
    ("bars = 1",             make_frame(10, 20, 0, 0.1, 4), 1),
    ("多列 (100列)",         make_frame(30, 40, 96, 0.15, 5), 25),
    ("1 銘柄のみ",           make_frame(1, 50, 2, 0.3, 6), 7),
]
for label, df, bars in cases:
    got = inc._trim(df.copy(), bars)
    want = trim_reference(df.copy(), bars)
    check(f"_trim {label}", got.equals(want),
          f"rows={len(got)}/{len(want)} cols={len(got.columns)}")

check("_trim 空フレームはそのまま", inc._trim(pd.DataFrame(), 10).empty)
no_key = pd.DataFrame({"a": [1, 2, 3]})
check("_trim キー列が無ければ素通し", inc._trim(no_key, 10).equals(no_key))

# ============================================================
print("[2] _frame_mb: サンプル外挿の誤差")
# ============================================================
for label, df in [
    ("小 (サンプル未満)", make_frame(10, 20, 5, 0.0, 7)),
    ("中",                make_frame(200, 60, 40, 0.0, 8)),
    ("大 (実サイズ相当)", make_frame(965, 105, 107, 0.0, 9)),
]:
    truth = float(df.memory_usage(deep=True).sum()) / 1048576.0
    t0 = time.perf_counter()
    got = inc._frame_mb(df)
    el = time.perf_counter() - t0
    err = abs(got - truth) / truth * 100.0
    check(f"_frame_mb {label} 誤差 < 2%", err < 2.0,
          f"truth={truth:.2f}MB got={got:.2f}MB err={err:.2f}% {el*1000:.1f}ms")

big = make_frame(965, 105, 107, 0.0, 9)
t0 = time.perf_counter(); float(big.memory_usage(deep=True).sum()); t_deep = time.perf_counter() - t0
t0 = time.perf_counter(); inc._frame_mb(big); t_new = time.perf_counter() - t0
check("_frame_mb は deep=True より 5 倍以上速い", t_new * 5 < t_deep,
      f"deep={t_deep*1000:.0f}ms new={t_new*1000:.0f}ms ({t_deep/max(t_new,1e-9):.1f}x)")
check("_frame_mb は空/None で落ちない",
      inc._frame_mb(pd.DataFrame()) == 0.0 and inc._frame_mb(None) == -1.0)

# ============================================================
print("[3] stats(): _LOCK を保持したまま重い集計をしない")
# ============================================================
inc.reset()
key = ("test", "20260828")
inc.store_full(key, make_frame(400, 105, 60, 0.0, 10))

lock_free = {"ok": None}
orig_frame_mb = inc._frame_mb


def probing_frame_mb(df):
    # 別スレッドから _LOCK を取れるか = ロック外で呼ばれているか
    if lock_free["ok"] is None:
        got = []

        def _t():
            got.append(inc._LOCK.acquire(timeout=2.0))
            if got[-1]:
                inc._LOCK.release()

        th = threading.Thread(target=_t, daemon=True)
        th.start()
        th.join(3.0)
        lock_free["ok"] = bool(got and got[0])
    return orig_frame_mb(df)


inc._frame_mb = probing_frame_mb
try:
    s = inc.stats()
finally:
    inc._frame_mb = orig_frame_mb

check("stats() は _frame_mb をロック外で呼ぶ", lock_free["ok"] is True)
check("stats() の entries が取れる", len(s["entries"]) == 1, f"entries={len(s['entries'])}")
e = s["entries"][0]
check("stats() symbols が正しい", e["symbols"] == 400, f"symbols={e['symbols']}")
check("stats() rows が正しい", e["rows"] == 400 * 105, f"rows={e['rows']}")
check("stats() memory_mb > 0", e["memory_mb"] > 0, f"mem={e['memory_mb']}MB")
check("stats() total_memory_mb > 0", s["total_memory_mb"] > 0)
check("stats() のキーが揃っている",
      {"enabled", "max_bars", "hits", "misses", "hit_rate", "full_reads",
       "delta_reads", "full_rows_read", "delta_rows_read", "entries"} <= set(s))

inc.log_stats("r18-test")
check("log_stats() が例外を出さない", True)

t0 = time.perf_counter()
for _ in range(5):
    inc.stats()
el = (time.perf_counter() - t0) / 5
check("stats() 1 回 < 0.15 秒", el < 0.15, f"{el*1000:.0f}ms")

inc.reset()

# ============================================================
print(f"\n結果: {PASS} / {PASS + FAIL}")
sys.exit(1 if FAIL else 0)
