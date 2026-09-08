# -*- coding: utf-8 -*-
"""
第18ラウンド: 3m/5m resample のベクトル化 (実注文なし)

2026-08-29 場中の実測で、3m/5m の base_df 15 秒のうち約 13 秒が
「562 銘柄を Python ループで 1 つずつ resample」だった。
ベクトル化しても **出力が 1 列も変わらない** ことを固定する。
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import numpy as np
import pandas as pd

import trading.ranking.summary.resample as rs

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


def make_one(n_sym=120, n_min=90, seed=0, gaps=False, dup=False):
    rng = np.random.default_rng(seed)
    t0 = pd.Timestamp("2026-08-29 09:00:00")
    syms = ["%04d" % (1000 + i) for i in range(n_sym)]
    dts = [t0 + pd.Timedelta(minutes=m) for m in range(n_min)]
    df = pd.DataFrame({
        "symbol": np.repeat(syms, n_min),
        "datetime": np.tile(dts, n_sym),
    })
    n = len(df)
    close = 1000 + rng.random(n) * 500
    df["open"] = close + rng.normal(0, 1, n)
    df["high"] = np.maximum(df["open"], close) + rng.random(n)
    df["low"] = np.minimum(df["open"], close) - rng.random(n)
    df["close"] = close
    df["volume"] = rng.integers(0, 5000, n).astype(float)
    df["trading_volume"] = np.cumsum(df["volume"]) % 1e6
    df["turnover"] = df["close"] * df["volume"]
    df["trading_value"] = df["turnover"]
    df["rank"] = rng.integers(1, 200, n).astype(float)
    df["best_rank"] = df["rank"]
    df["symbolname"] = "N" + df["symbol"]
    df["ranking_type"] = "値上がり率"
    df["rank_type"] = df["ranking_type"]
    df["category"] = df["ranking_type"]
    df["market"] = "TP"
    df["market_type"] = "TP"
    df["change_percentage"] = rng.normal(0, 2, n)
    df["source"] = "ranking_summary_snapshot_1m"
    df["price_source"] = "ranking_snapshot"
    df["mode"] = "summary"
    if gaps:
        # 時間に穴を空ける (resample が空バケットを作る条件)
        keep = rng.random(len(df)) > 0.35
        df = df[keep].reset_index(drop=True)
    if dup:
        df = pd.concat([df, df.iloc[:200]], ignore_index=True)
    return df


def loop_reference(one, interval):
    """R18 以前の実装 (期待値)。"""
    frames = []
    for symbol, g in one.groupby("symbol", sort=False):
        r = rs.resample_symbol_frame(g, interval=interval)
        if r is not None and not r.empty:
            frames.append(r)
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True)
    out = out.sort_values(["symbol", "datetime"], ascending=[True, True],
                          kind="mergesort").reset_index(drop=True)
    return rs._ensure_summary_base_columns(out, interval=interval)


def compare(a, b, label):
    if len(a) != len(b):
        check(f"{label} 行数一致", False, f"{len(a)} vs {len(b)}")
        return
    check(f"{label} 行数一致", True, f"rows={len(a)}")
    only_a = sorted(set(a.columns) - set(b.columns))
    only_b = sorted(set(b.columns) - set(a.columns))
    check(f"{label} 列集合一致", not only_a and not only_b,
          f"旧のみ={only_a} 新のみ={only_b}")
    cols = sorted(set(a.columns) & set(b.columns))
    aa = a[cols].reset_index(drop=True)
    bb = b[cols].reset_index(drop=True)
    diff = []
    for c in cols:
        sa, sb = aa[c], bb[c]
        if pd.api.types.is_numeric_dtype(sa) and pd.api.types.is_numeric_dtype(sb):
            if not np.allclose(sa.fillna(-9e99).to_numpy(dtype=float),
                               sb.fillna(-9e99).to_numpy(dtype=float),
                               rtol=1e-9, atol=1e-9, equal_nan=True):
                diff.append(c)
        else:
            if not sa.fillna("~").astype(str).equals(sb.fillna("~").astype(str)):
                diff.append(c)
    check(f"{label} 全 {len(cols)} 列の値一致", not diff, str(diff[:5]))


# ============================================================
print("[1] 旧ループと同一の出力になる")
# ============================================================
for label, kw in [
    ("通常", {}),
    ("時間に穴あり", {"gaps": True, "seed": 1}),
    ("重複行あり", {"dup": True, "seed": 2}),
    ("1 銘柄のみ", {"n_sym": 1, "seed": 3}),
    ("短い期間", {"n_min": 4, "seed": 4}),
]:
    one = make_one(**kw)
    for interval in (3, 5):
        a = loop_reference(one, interval)
        b = rs.resample_all_symbols(one, interval=interval)
        compare(a, b, f"{label} interval={interval}")

# ============================================================
print("[2] 速度")
# ============================================================
one = make_one(n_sym=562, n_min=120, seed=9)
for interval in (3, 5):
    t0 = time.perf_counter(); a = loop_reference(one, interval); el_a = time.perf_counter() - t0
    t0 = time.perf_counter(); b = rs.resample_all_symbols(one, interval=interval); el_b = time.perf_counter() - t0
    check(f"interval={interval} は 5 倍以上速い", el_a > el_b * 5,
          f"ループ {el_a:.2f}s -> ベクトル {el_b:.2f}s ({el_a/max(el_b,1e-9):.1f} 倍)")
    compare(a, b, f"562 銘柄 interval={interval}")

# ============================================================
print("[3] 縮退・env")
# ============================================================
check("空フレームは空を返す", rs.resample_all_symbols(pd.DataFrame(), interval=3).empty)
check("None は空を返す", rs.resample_all_symbols(None, interval=3).empty)

os.environ["RANKING_RESAMPLE_VECTORIZED"] = "0"
check("env=0 で無効化できる", rs._resample_vectorized_enabled() is False)
os.environ["RANKING_RESAMPLE_VECTORIZED"] = "1"
check("env=1 で有効", rs._resample_vectorized_enabled() is True)
os.environ.pop("RANKING_RESAMPLE_VECTORIZED", None)
check("既定は有効", rs._resample_vectorized_enabled() is True)

# datetime が壊れている行は落ちる (旧実装と同じ)
one = make_one(n_sym=3, n_min=10, seed=5)
one.loc[0, "datetime"] = pd.NaT
a = loop_reference(one, 3)
b = rs.resample_all_symbols(one, interval=3)
compare(a, b, "datetime に NaT")

print(f"\n結果: {PASS} / {PASS + FAIL}")
sys.exit(1 if FAIL else 0)
