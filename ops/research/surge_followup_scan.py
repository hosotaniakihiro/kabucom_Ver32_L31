# ============================================================
# ops/research/surge_followup_scan.py
# 2026-10-20: 3042 (前日 1,100 円 -> 09:15 1,377 円 -> 12:40 から崩れて 13:02 1,024 円) を取りこぼした件の検証。
#   ユーザー: 「3042 は取れないとまずいね」
#
# (1) SURGE SHORT の見送り銘柄: +12% を初めて超えた時にストップ高まで 5% 未満で見送った銘柄を、
#     その後 (14:30 まで) ストップ高から 5% 以上離れた最初の足の次の始値で売る。今の売り (離れていた銘柄) と比べる。
#     出口は SURGE SHORT と同じ: +5% 損切り / 含み益 1% 超から最安値 +0.5% 戻しで利確 / -5% 利確 / 30 分。
# (2) 急騰した日の崩れ: +12% を超えた後、60 分以上たってから、超えた後の最安値 (もみ合いの下限) を
#     終値で割った最初の足の次の始値で売る (10:30〜14:30)。出口: 株価が 5MA を上に抜けたら (直前確定の MA5 に
#     高値が触れたら) / 30 分 / 60 分 / 大引け。損切り +5%。往復 0.10% (プレミアム料は含まない)。
# ============================================================
from __future__ import annotations

import datetime as dt
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from trading.strategies.surge_short import limit_up_price  # noqa: E402

LOCAL = os.path.join(os.getenv("LOCALAPPDATA") or r"C:\Temp", "autostock")
CACHE = os.path.join(LOCAL, "research", "ma75_turnaround", "bars5m.pkl")
COST = 0.0010
T_START, T_END = dt.time(9, 15), dt.time(14, 30)


def load() -> pd.DataFrame:
    b = pd.read_pickle(CACHE).sort_values(["symbol", "datetime"]).reset_index(drop=True)
    b["d"] = b["datetime"].dt.normalize()
    days = b.groupby("d")["symbol"].nunique()
    b = b[b["d"].isin(days[days >= 100].index)]
    b["tv"] = b["close"] * b["volume"]
    daily = b.groupby(["symbol", "d"]).agg(c=("close", "last"), tv=("tv", "sum")).reset_index()
    daily["prev_c"] = daily.groupby("symbol")["c"].shift(1)
    keep = daily[(daily["prev_c"] >= 300)][["symbol", "d", "prev_c"]]
    b = b.merge(keep, on=["symbol", "d"]).sort_values(["symbol", "datetime"]).reset_index(drop=True)
    b["ma5"] = b.groupby("symbol")["close"].transform(lambda s: s.rolling(5).mean())
    return b


def surge_exit(o, h, l, c, e_i: int, entry: float) -> float:
    """SURGE SHORT の出口 (売り)。戻り値は損益率 (コスト込み)。"""
    best = entry
    last = min(len(c) - 1, e_i + 5)  # 30 分 = 6 本
    for k in range(e_i, last + 1):
        if h[k] >= entry * 1.05:
            return entry / max(o[k], entry * 1.05) - 1 - COST
        if l[k] <= entry * 0.95:
            return entry / min(o[k], entry * 0.95) - 1 - COST
        if best <= entry * 0.99 and h[k] >= best * 1.005:
            return entry / max(o[k], best * 1.005) - 1 - COST
        best = min(best, l[k])
    return entry / c[last] - 1 - COST


def collapse_exits(o, h, l, c, ma5, e_i: int, entry: float) -> dict:
    out = {}
    n = len(c)
    for name, horizon in (("r_ma5", None), ("r30", 6), ("r60", 12), ("r_close", None)):
        last = n - 1 if horizon is None else min(n - 1, e_i + horizon - 1)
        px = None
        for k in range(e_i, last + 1):
            if h[k] >= entry * 1.05:
                px = max(o[k], entry * 1.05); break
            if name == "r_ma5" and k > e_i and not np.isnan(ma5[k - 1]) and h[k] >= ma5[k - 1]:
                px = max(o[k], ma5[k - 1]); break
        out[name] = entry / (px if px is not None else c[last]) - 1 - COST
    return out


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    pd.set_option("display.width", 220)
    b = load()
    print(f"期間 {b['d'].min().date()}..{b['d'].max().date()}  コスト 0.10% 込み (プレミアム料は含まない)", flush=True)
    s1, s2 = [], []
    for (sym, d), g in b.groupby(["symbol", "d"], sort=False):
        pc = float(g["prev_c"].iloc[0])
        c = g["close"].to_numpy(float)
        if c.max() < pc * 1.12:
            continue
        o, h, l, ma5 = (g[k].to_numpy(float) for k in ("open", "high", "low", "ma5"))
        tm = g["datetime"].dt.time.to_numpy()
        n = len(c)
        lu = limit_up_price(pc)
        i = next((k for k in range(n - 1) if c[k] >= pc * 1.12 and T_START <= tm[k] <= T_END), None)
        if i is None:
            continue
        dist = (lu / c[i] - 1) * 100
        if dist >= 5:
            s1.append({"kind": "今の売り (離れていた)", "d": d, "symbol": sym, "r": surge_exit(o, h, l, c, i + 1, o[i + 1])})
        else:
            k = next((k for k in range(i + 1, n - 1) if tm[k] <= T_END and c[k] <= lu / 1.05), None)
            if k is not None:
                s1.append({"kind": "見送り -> 離れたら売る", "d": d, "symbol": sym, "r": surge_exit(o, h, l, c, k + 1, o[k + 1]),
                           "wait_min": (k - i) * 5})
        # (2) 崩れ
        for k in range(i + 12, n - 1):
            if not (dt.time(10, 30) <= tm[k] <= T_END):
                continue
            rng_low = l[i:k].min()
            if c[k] < rng_low:
                r = collapse_exits(o, h, l, c, ma5, k + 1, o[k + 1])
                s2.append({"d": d, "symbol": sym, "t": tm[k], "day_chg_at_entry": (c[k] / pc - 1) * 100,
                           "from_high": (c[k] / h[i:k + 1].max() - 1) * 100, **r})
                break
    e1 = pd.DataFrame(s1)
    half = b["d"].quantile(0.5)
    print("\n===== (1) SURGE SHORT: ストップ高に近くて見送った銘柄を、離れてから売った場合 =====")
    print(e1.groupby("kind").agg(件数=("r", "size"), 平均=("r", lambda x: x.mean() * 100), 勝率=("r", lambda x: (x > 0).mean() * 100),
                                 最悪=("r", lambda x: x.min() * 100),
                                 前半=("r", lambda x: x[e1.loc[x.index, "d"] < half].mean() * 100),
                                 後半=("r", lambda x: x[e1.loc[x.index, "d"] >= half].mean() * 100)).round(3).to_string())
    e2 = pd.DataFrame(s2)
    print(f"\n===== (2) 急騰した日の崩れ (もみ合いの下限割れ) で売り  件数 {len(e2)} =====")
    rows = []
    for k, nm in (("r_ma5", "5MA 上抜けで買い戻し"), ("r30", "30 分"), ("r60", "60 分"), ("r_close", "大引け")):
        x = e2[k]
        rows.append({"出口": nm, "平均%": x.mean() * 100, "勝率": (x > 0).mean() * 100, "最悪%": x.min() * 100,
                     "前半%": x[e2["d"] < half].mean() * 100, "後半%": x[e2["d"] >= half].mean() * 100})
    print(pd.DataFrame(rows).round(3).to_string(index=False))
    e2["上昇率"] = pd.cut(e2["day_chg_at_entry"], [-100, 5, 10, 20, 1000], labels=["+5%未満", "+5〜10%", "+10〜20%", "+20%超"])
    print("\n-- 崩れた時点の当日上昇率別 (大引けで買い戻し / 5MA)")
    print(e2.groupby("上昇率", observed=True).agg(件数=("r_close", "size"), 大引け=("r_close", lambda x: x.mean() * 100),
                                                  五MA=("r_ma5", lambda x: x.mean() * 100),
                                                  勝率大引け=("r_close", lambda x: (x > 0).mean() * 100)).round(3).to_string())
    e1.to_pickle(os.path.join(LOCAL, "research", "surge_followup_1.pkl"))
    e2.to_pickle(os.path.join(LOCAL, "research", "surge_followup_2.pkl"))


if __name__ == "__main__":
    main()
