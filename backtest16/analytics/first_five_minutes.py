# ============================================================
# backtest16/analytics/first_five_minutes.py
# ------------------------------------------------------------
# §13 最重要の独立分析: 寄り付きギャップ × 最初の5分足の形から、
# その後の上昇/下落確率がどう変わるかを、元データから検証する。
#
# 戦略とは独立に、全 SymbolDay について 09:05 確定後の値動きを集計する。
# イベント時系列(ティック)が無い区間では先後関係を「不明」にする(§13注記)。
# ============================================================

from __future__ import annotations

from typing import List, Optional

import pandas as pd

from backtest16 import config as C
from backtest16.features import SymbolDay, gap_bucket


def analyze_symbol_day(sd: SymbolDay, ref_move_pct: float = 0.3) -> Optional[dict]:
    """1銘柄日の初動5分足後の値動きを1レコードに。first5が取れない日は None。"""
    if not sd.first5_ok or sd.first5_high is None or sd.first5_low is None:
        return None
    hi, lo = sd.first5_high, sd.first5_low
    # 基準価格 = 最初の5分足の終値(≒09:05時点)。1分足09:04の close で近似。
    b1 = sd.bars1
    if b1 is None or b1.empty:
        return None
    b1 = b1.sort_values("ts")
    pre = b1[b1["ts"].dt.time < C.FIRST_BAR_LOCK]
    ref = float(pre.iloc[-1]["close"]) if not pre.empty else sd.day_open
    if not ref or ref <= 0:
        return None

    rec = {
        "symbol": sd.symbol, "date": str(sd.date),
        "gap_pct": sd.gap_pct, "gap_bucket": gap_bucket(sd.gap_pct),
        "first5_bullish": sd.first5_bullish,
        "high_first": None, "low_first": None,
        "max_up_pct": 0.0, "max_down_pct": 0.0,
        "reached_up_first": None, "reached_down_first": None,
        "reversal_seconds": None, "source": "OHLC",
    }

    # 09:05 以降のパス: ティックがあればティック、無ければ1分足
    tk = sd.ticks[sd.ticks["ts"].dt.time >= C.FIRST_BAR_LOCK] if (sd.ticks is not None and not sd.ticks.empty) else None
    up_thr = ref * (1 + ref_move_pct / 100.0)
    dn_thr = ref * (1 - ref_move_pct / 100.0)

    if tk is not None and not tk.empty:
        rec["source"] = "TICK"
        hi_break_ts = lo_break_ts = None
        up_ts = dn_ts = None
        mx = mn = ref
        for _, r in tk.iterrows():
            px = float(r["price"]) if pd.notna(r["price"]) else None
            if px is None:
                continue
            mx = max(mx, px); mn = min(mn, px)
            if hi_break_ts is None and px > hi:
                hi_break_ts = r["ts"]
            if lo_break_ts is None and px < lo:
                lo_break_ts = r["ts"]
            if up_ts is None and px >= up_thr:
                up_ts = r["ts"]
            if dn_ts is None and px <= dn_thr:
                dn_ts = r["ts"]
        rec["max_up_pct"] = round((mx - ref) / ref * 100.0, 3)
        rec["max_down_pct"] = round((mn - ref) / ref * 100.0, 3)
        if hi_break_ts or lo_break_ts:
            if hi_break_ts and (lo_break_ts is None or hi_break_ts <= lo_break_ts):
                rec["high_first"] = True; rec["low_first"] = False
            else:
                rec["high_first"] = False; rec["low_first"] = True
        if up_ts or dn_ts:
            rec["reached_up_first"] = bool(up_ts and (dn_ts is None or up_ts <= dn_ts))
            rec["reached_down_first"] = not rec["reached_up_first"]
    else:
        # 1分足: 足のhigh/lowで先後を近似。同一足で両方跨ぐ場合は「不明」。
        post = b1[b1["ts"].dt.time >= C.FIRST_BAR_LOCK]
        mx = mn = ref
        hi_first = lo_first = None
        up_first = dn_first = None
        for _, bar in post.iterrows():
            h, l = float(bar["high"]), float(bar["low"])
            mx = max(mx, h); mn = min(mn, l)
            broke_hi = h > hi; broke_lo = l < lo
            if hi_first is None and lo_first is None:
                if broke_hi and not broke_lo:
                    hi_first, lo_first = True, False
                elif broke_lo and not broke_hi:
                    hi_first, lo_first = False, True
                # 同一足で両方 → 先後不明のまま(None)
            up = h >= up_thr; dn = l <= dn_thr
            if up_first is None and dn_first is None:
                if up and not dn:
                    up_first, dn_first = True, False
                elif dn and not up:
                    up_first, dn_first = False, True
        rec["max_up_pct"] = round((mx - ref) / ref * 100.0, 3)
        rec["max_down_pct"] = round((mn - ref) / ref * 100.0, 3)
        rec["high_first"], rec["low_first"] = hi_first, lo_first
        rec["reached_up_first"], rec["reached_down_first"] = up_first, dn_first
    return rec


def aggregate(records: List[dict]) -> dict:
    """gap_bucket × first5方向 で集計。"""
    df = pd.DataFrame([r for r in records if r])
    if df.empty:
        return {"n": 0}
    df["dir"] = df["first5_bullish"].map({True: "陽線", False: "陰線"})
    # 分割/異常ティック由来の外れ値(例 +248%)が平均を歪めるため、
    # 平均は ±20% にwinsorizeし、外れ値件数は別途カウントして明示する(§3 データ品質)。
    CLIP = 20.0
    out = {"n": int(len(df)), "groups": {},
           "outliers_abs_gt_20pct": int(((df["max_up_pct"].abs() > CLIP) | (df["max_down_pct"].abs() > CLIP)).sum()),
           "note": "avg_max_*_pct は±20%でwinsorize(外れ値=分割/異常ティックの可能性)。先後率は未加工。"}
    for (gb, d), g in df.groupby(["gap_bucket", "dir"]):
        n = len(g)
        def frac(col):
            s = g[col].dropna()
            return round(float(s.mean()), 4) if len(s) else None
        up_w = g["max_up_pct"].clip(-CLIP, CLIP)
        dn_w = g["max_down_pct"].clip(-CLIP, CLIP)
        out["groups"][f"{gb} / {d}"] = {
            "n": n,
            "high_first_rate": frac("high_first"),
            "low_first_rate": frac("low_first"),
            "up_0.3_first_rate": frac("reached_up_first"),
            "down_0.3_first_rate": frac("reached_down_first"),
            "avg_max_up_pct": round(float(up_w.mean()), 3),
            "avg_max_down_pct": round(float(dn_w.mean()), 3),
            "outliers": int(((g["max_up_pct"].abs() > CLIP) | (g["max_down_pct"].abs() > CLIP)).sum()),
            "tick_source_rate": round(float((g["source"] == "TICK").mean()), 3),
        }
    return out
