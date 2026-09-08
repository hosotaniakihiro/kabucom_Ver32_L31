# -*- coding: utf-8 -*-
"""
第18ラウンド: technical の per-symbol ループのベクトル化 (実注文なし)

add_ranking_indicators / add_ranking_indicator_flags は 562 銘柄を
Python ループで回し、銘柄ごとに copy + sort_values をやり直して
最後に 562 フレームを concat していた。
ベクトル化しても **1 行 1 列も変わらない** ことを固定する。
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import numpy as np
import pandas as pd

import trading.ranking.technical_from_ranking as tf

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


def make_price(n_sym=120, n_min=120, seed=0, gaps=False, nan_close=False):
    rng = np.random.default_rng(seed)
    t0 = pd.Timestamp("2026-08-29 09:00:00")
    syms = ["%04d" % (1000 + i) for i in range(n_sym)]
    dts = [t0 + pd.Timedelta(minutes=m) for m in range(n_min)]
    df = pd.DataFrame({
        "symbol": np.repeat(syms, n_min),
        "datetime": np.tile(dts, n_sym),
    })
    n = len(df)
    walk = rng.normal(0, 1.5, n).cumsum()
    df["close"] = 1000 + walk % 400
    if gaps:
        df = df[rng.random(len(df)) > 0.3].reset_index(drop=True)
    if nan_close:
        df.loc[rng.random(len(df)) < 0.05, "close"] = np.nan
    return df


# ---- R18 以前の実装 (期待値) ----
def ref_add_indicators(price_df, *, rsi_period=14, macd_fast=12, macd_slow=26,
                       macd_signal=9):
    df = price_df.copy()
    df["symbol"] = tf._normalize_symbol_series(df["symbol"])
    df["datetime"] = tf._safe_to_datetime(df["datetime"])
    df["close"] = tf._as_numeric(df["close"])
    df = df.dropna(subset=["symbol", "datetime", "close"])
    df = df.sort_values(["symbol", "datetime"]).reset_index(drop=True)

    pieces = []
    for symbol, g in df.groupby("symbol", sort=False):
        one = g.copy().sort_values("datetime")
        close = one["close"]
        one["rsi"] = tf._calc_rsi(close, period=rsi_period)
        macd, sig, hist = tf._calc_macd(close, fast=macd_fast, slow=macd_slow,
                                        signal=macd_signal)
        one["macd"] = macd
        one["macd_signal"] = sig
        one["macd_hist"] = hist
        one["rsi_slope"] = one["rsi"].diff()
        one["macd_hist_slope"] = one["macd_hist"].diff()
        pieces.append(one)
    if not pieces:
        return pd.DataFrame()
    out = pd.concat(pieces, ignore_index=True)
    return out.sort_values(["symbol", "datetime"]).reset_index(drop=True)


def ref_add_flags(out):
    out = out.copy()
    pieces = []
    for symbol, g in out.groupby("symbol", sort=False):
        one = g.copy().sort_values("datetime")
        prev_macd = one["macd"].shift(1)
        prev_sig = one["macd_signal"].shift(1)
        prev_hist = one["macd_hist"].shift(1)
        prev_rsi = one["rsi"].shift(1)
        one["flag_macd_cross"] = (prev_macd <= prev_sig) & (one["macd"] > one["macd_signal"])
        one["flag_macd_dc"] = (prev_macd >= prev_sig) & (one["macd"] < one["macd_signal"])
        one["flag_macd_hist_expand"] = (one["macd_hist"] > 0) & (one["macd_hist"] > prev_hist)
        one["flag_macd_hist_contract"] = (one["macd_hist"] > 0) & (one["macd_hist"] < prev_hist)
        one["flag_rsi_rebound"] = (prev_rsi < 40) & (one["rsi"] >= 40)
        one["flag_rsi_midline_cross"] = (prev_rsi <= 50) & (one["rsi"] > 50)
        one["flag_rsi_falling"] = one["rsi"] < prev_rsi
        one["flag_rsi_overbought_70"] = one["rsi"] >= 70
        pieces.append(one)
    out = pd.concat(pieces, ignore_index=True)
    return out.sort_values(["symbol", "datetime"]).reset_index(drop=True)


def compare(a, b, label):
    if len(a) != len(b):
        check(f"{label} 行数一致", False, f"{len(a)} vs {len(b)}")
        return False
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
            if not np.allclose(sa.to_numpy(dtype=float), sb.to_numpy(dtype=float),
                               rtol=1e-9, atol=1e-9, equal_nan=True):
                diff.append(c)
        else:
            if not sa.fillna("~").astype(str).equals(sb.fillna("~").astype(str)):
                diff.append(c)
    check(f"{label} 全 {len(cols)} 列の値一致", not diff, str(diff[:5]))
    return not diff


# ============================================================
print("[1] add_ranking_indicators が旧実装と一致")
# ============================================================
for label, kw in [
    ("通常", {}),
    ("時間に穴あり", {"gaps": True, "seed": 1}),
    ("close に NaN", {"nan_close": True, "seed": 2}),
    ("1 銘柄のみ", {"n_sym": 1, "seed": 3}),
    ("短い系列 (指標が立たない)", {"n_min": 5, "seed": 4}),
]:
    p = make_price(**kw)
    a = ref_add_indicators(p)
    b = tf.add_ranking_indicators(p)
    compare(a, b, f"indicators {label}")

# ============================================================
print("[2] add_ranking_indicator_flags が旧実装と一致")
# ============================================================
for label, kw in [
    ("通常", {}),
    ("時間に穴あり", {"gaps": True, "seed": 5}),
    ("1 銘柄のみ", {"n_sym": 1, "seed": 6}),
]:
    p = make_price(**kw)
    base = ref_add_indicators(p)
    a = ref_add_flags(base)
    b = tf.add_ranking_indicator_flags(base.copy())
    compare(a, b, f"flags {label}")

# ============================================================
print("[3] 速度 (562 銘柄 x 120 分)")
# ============================================================
p = make_price(n_sym=562, n_min=120, seed=9)
t0 = time.perf_counter(); a = ref_add_indicators(p); el_a = time.perf_counter() - t0
t0 = time.perf_counter(); b = tf.add_ranking_indicators(p); el_b = time.perf_counter() - t0
check("indicators が速くなっている", el_b < el_a,
      f"ループ {el_a:.2f}s -> 新 {el_b:.2f}s ({el_a/max(el_b,1e-9):.1f} 倍)")
compare(a, b, "562 銘柄 indicators")

t0 = time.perf_counter(); c = ref_add_flags(a); el_c = time.perf_counter() - t0
t0 = time.perf_counter(); d = tf.add_ranking_indicator_flags(a.copy()); el_d = time.perf_counter() - t0
check("flags が 5 倍以上速い", el_c > el_d * 5,
      f"ループ {el_c:.2f}s -> 新 {el_d:.2f}s ({el_c/max(el_d,1e-9):.1f} 倍)")
compare(c, d, "562 銘柄 flags")

# ============================================================
print("[4] 縮退")
# ============================================================
check("空フレームで落ちない (indicators)",
      tf.add_ranking_indicators(pd.DataFrame()).empty)
try:
    r = tf.add_ranking_indicator_flags(pd.DataFrame())
    check("空フレームで落ちない (flags)", r is None or r.empty)
except Exception as e:
    check("空フレームで落ちない (flags)", False, f"{type(e).__name__}: {e}")

print(f"\n結果: {PASS} / {PASS + FAIL}")
sys.exit(1 if FAIL else 0)
