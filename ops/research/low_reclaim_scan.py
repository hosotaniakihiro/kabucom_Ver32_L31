# ============================================================
# ops/research/low_reclaim_scan.py
# 2026-10-20: 当日安値を時間をおいて更新し、すぐ元の安値の上へ戻したら買う (研究用)。
#   ユーザー: 「その日の安値を時間をおいて安値を更新し、すぐにその安値を回復した場合はしばらく上昇する可能性がある」
#
# 5 分足 (bars5m.pkl)、前日売買代金 5 億円以上・株価 300 円以上。
#   L   = 足 i の直前までの当日安値、tL = その安値をつけた足の時刻
#   更新: 足 i の安値 < L かつ (足 i の時刻 - tL) >= GAP 分
#   回復: 足 i〜i+R-1 のどこかで終値 > L (最初にそうなった足 j)。次の足 j+1 の始値で買う
#   損切り: 足 i〜j の最安値 (新しい安値) を割ったらその値段
#   出口: (a) 株価が 5MA を割ったら (直前確定までの MA5 に安値が触れたら、その値段 / 始値が下なら始値)
#         (b) 30 分後 (c) 60 分後 (b,c も損切りあり)。どれも無ければその日の最後の終値。往復 0.10%。
# ============================================================
from __future__ import annotations

import datetime as dt
import os
import sys

import numpy as np
import pandas as pd

LOCAL = os.path.join(os.getenv("LOCALAPPDATA") or r"C:\Temp", "autostock")
CACHE = os.path.join(LOCAL, "research", "ma75_turnaround", "bars5m.pkl")
COST = 0.0010


def load() -> pd.DataFrame:
    b = pd.read_pickle(CACHE).sort_values(["symbol", "datetime"]).reset_index(drop=True)
    b["d"] = b["datetime"].dt.normalize()
    days = b.groupby("d")["symbol"].nunique()
    b = b[b["d"].isin(days[days >= 100].index)]
    b["tv"] = b["close"] * b["volume"]
    daily = b.groupby(["symbol", "d"]).agg(c=("close", "last"), tv=("tv", "sum")).reset_index()
    daily["prev_tv"] = daily.groupby("symbol")["tv"].shift(1)
    daily["prev_c"] = daily.groupby("symbol")["c"].shift(1)
    keep = daily[(daily["prev_tv"] >= 5e8) & (daily["prev_c"] >= 300)][["symbol", "d", "prev_c"]]
    b = b.merge(keep, on=["symbol", "d"]).sort_values(["symbol", "datetime"]).reset_index(drop=True)
    b["ma5"] = b.groupby("symbol")["close"].transform(lambda s: s.rolling(5).mean())
    return b


def scan_day(g: pd.DataFrame, gap_min: int, R: int) -> list:
    t = g["datetime"].to_numpy()
    o, h, l, c, ma5 = (g[k].to_numpy(float) for k in ("open", "high", "low", "close", "ma5"))
    n = len(g)
    out = []
    low, low_i = l[0], 0
    i = 1
    gap = np.timedelta64(gap_min, "m")
    while i < n - 1:
        if l[i] < low and (t[i] - t[low_i]) >= gap:
            L = low
            j = next((k for k in range(i, min(i + R, n - 1)) if c[k] > L), None)
            if j is not None and j + 1 < n:
                e_i = j + 1
                et = pd.Timestamp(t[e_i]).time()
                if dt.time(9, 30) <= et <= dt.time(14, 30):
                    entry = o[e_i]
                    stop = l[i:j + 1].min()
                    res = {"t": pd.Timestamp(t[e_i]), "entry": entry, "depth": (L / stop - 1) * 100,
                           "stop_pct": (stop / entry - 1) * 100, "gap_min": (t[i] - t[low_i]) / np.timedelta64(1, "m")}
                    # 出口
                    for name, horizon in (("r30", 6), ("r60", 12), ("rma5", None)):
                        px = None
                        last = n - 1 if horizon is None else min(n - 1, e_i + horizon - 1)
                        for k in range(e_i, last + 1):
                            if l[k] <= stop:
                                px = min(o[k], stop); break
                            if horizon is None and k > e_i and not np.isnan(ma5[k - 1]) and l[k] <= ma5[k - 1]:
                                px = min(o[k], ma5[k - 1]); break
                        if px is None:
                            px = c[last]
                        res[name] = px / entry - 1 - COST
                    out.append(res)
                    i = e_i  # 1 回使ったら次へ
                    low, low_i = min(low, l[i:e_i + 1].min() if e_i > i else l[i]), i
                    continue
        if l[i] < low:
            low, low_i = l[i], i
        i += 1
    return out


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    pd.set_option("display.width", 220)
    b = load()
    print(f"期間 {b['d'].min().date()}..{b['d'].max().date()}  銘柄日 {b.groupby(['symbol','d']).ngroups:,}  コスト 0.10% 込み", flush=True)
    groups = [(k, g.reset_index(drop=True)) for k, g in b.groupby(["symbol", "d"], sort=False) if len(g) >= 20]
    half = b["d"].quantile(0.5)
    rows, keep = [], {}
    for gap in (15, 30, 60):
        for R in (1, 2, 3):
            ev = []
            for (sym, d), g in groups:
                for r in scan_day(g, gap, R):
                    r["symbol"], r["d"] = sym, d
                    ev.append(r)
            ev = pd.DataFrame(ev)
            keep[(gap, R)] = ev
            s = {"更新までの間隔(分)": gap, "回復までの本数": R, "件数": len(ev), "銘柄": ev["symbol"].nunique()}
            for k, nm in (("rma5", "5MA利確"), ("r30", "30分"), ("r60", "60分")):
                s[f"{nm}%"] = ev[k].mean() * 100
                s[f"勝率{nm}"] = (ev[k] > 0).mean() * 100
            s["5MA前半%"] = ev.loc[ev["d"] < half, "rma5"].mean() * 100
            s["5MA後半%"] = ev.loc[ev["d"] >= half, "rma5"].mean() * 100
            s["損切り幅%"] = ev["stop_pct"].median()
            rows.append(s)
            print(gap, R, len(ev), round(s["5MA利確%"], 3), round(s["60分%"], 3), flush=True)
    t = pd.DataFrame(rows).round(3)
    print("\n===== 条件ごとの成績 (コスト 0.10% 込み) =====")
    print(t.to_string(index=False))
    ev = keep[(30, 2)]
    ev["深さ"] = pd.cut(ev["depth"], [0, 0.3, 1.0, 100], labels=["0〜0.3%", "0.3〜1%", "1%超"])
    ev["時間帯"] = pd.cut(ev["t"].dt.hour * 60 + ev["t"].dt.minute, [0, 11 * 60 + 30, 15 * 60], labels=["前場", "後場"])
    for col in ("深さ", "時間帯"):
        print(f"\n===== 間隔 30 分・回復 2 本以内を {col} 別に =====")
        print(ev.groupby(col, observed=True).agg(件数=("rma5", "size"), 五MA利確=("rma5", lambda x: x.mean() * 100),
                                                 勝率=("rma5", lambda x: (x > 0).mean() * 100),
                                                 六十分=("r60", lambda x: x.mean() * 100)).round(3).to_string())
    pd.concat({f"{k[0]}_{k[1]}": v for k, v in keep.items()}).to_pickle(os.path.join(LOCAL, "research", "low_reclaim_events.pkl"))


if __name__ == "__main__":
    main()
