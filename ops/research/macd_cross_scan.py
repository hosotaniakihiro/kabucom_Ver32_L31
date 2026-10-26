# ============================================================
# ops/research/macd_cross_scan.py
# 2026-10-19: 「MACD がマイナスでクロスしたときにエントリーしたい」(ユーザー) を 5 分足で検証 (研究用)。
#
# 買い: MACD がシグナルを上抜け、その時点で MACD < 0 (深さ別: MACD/終値 が -0.1% 未満 / -0.3% 未満 など)
# 売り (参考): MACD がシグナルを下抜け、その時点で MACD > 0
# 次の足の始値で入り、30 / 60 分後の終値で出る。往復コスト 0.10%。9:30〜14:00、5 分売買代金平均 500 万円以上。
# 追加の条件: 25MA より上 / 75MA の傾きが上 / 当日の下げ幅。
# ============================================================
from __future__ import annotations

import datetime as dt
import os

import numpy as np
import pandas as pd

LOCAL = os.path.join(os.getenv("LOCALAPPDATA") or r"C:\Temp", "autostock")
CACHE = os.path.join(LOCAL, "research", "ma75_turnaround", "bars5m.pkl")
COST = 0.0010


def main() -> None:
    bars = pd.read_pickle(CACHE).sort_values(["symbol", "datetime"]).reset_index(drop=True)
    days = bars.groupby(bars["datetime"].dt.normalize())["symbol"].nunique()
    bars = bars[bars["datetime"].dt.normalize().isin(set(days[days >= 100].index))]
    t_lo, t_hi = dt.time(9, 30), dt.time(14, 0)
    out = []
    for sym, g in bars.groupby("symbol", sort=False):
        if len(g) < 80:
            continue
        g = g.reset_index(drop=True)
        c = g["close"]
        day = g["datetime"].dt.normalize()
        macd = c.ewm(span=12, adjust=False).mean() - c.ewm(span=26, adjust=False).mean()
        sig = macd.ewm(span=9, adjust=False).mean()
        up = (macd > sig) & (macd.shift(1) <= sig.shift(1))
        dn = (macd < sig) & (macd.shift(1) >= sig.shift(1))
        depth = macd / c
        ma25 = c.rolling(25, min_periods=25).mean()
        ma75 = c.rolling(75, min_periods=75).mean()
        slope75 = ma75 / ma75.shift(6) - 1
        from_open = c / g.groupby(day)["open"].transform("first") - 1
        turnover = (c * g["volume"]).rolling(12, min_periods=12).mean().shift(1)
        nxt = g["open"].shift(-1)
        ok12 = day.shift(-12) == day
        tm = g["datetime"].dt.time
        base = (tm >= t_lo) & (tm <= t_hi) & (turnover >= 5_000_000) & (nxt > 0) & ok12
        for side, m, sgn in (("BUY", up & (macd < 0), 1.0), ("SELL", dn & (macd > 0), -1.0), ("BUY_ANY", up, 1.0)):
            idx = np.flatnonzero((m & base).to_numpy())
            if not len(idx):
                continue
            out.append(pd.DataFrame({
                "side": side, "datetime": g["datetime"].to_numpy()[idx],
                "depth": sgn * depth.to_numpy()[idx],
                "above25": sgn * (c / ma25 - 1).to_numpy()[idx],
                "slope75": sgn * slope75.to_numpy()[idx],
                "from_open": sgn * from_open.to_numpy()[idx],
                "r30": sgn * (c.shift(-6) / nxt - 1).to_numpy()[idx] - COST,
                "r60": sgn * (c.shift(-12) / nxt - 1).to_numpy()[idx] - COST,
            }))
    ev = pd.concat(out, ignore_index=True).dropna(subset=["r30", "r60"])
    ev = ev[(ev["r30"].abs() < 0.3) & (ev["r60"].abs() < 0.3)]
    mid = ev["datetime"].quantile(0.5)
    ev["half"] = np.where(ev["datetime"] < mid, "H1", "H2")

    def s(x: pd.DataFrame) -> pd.Series:
        pos, neg = x.loc[x.r60 > 0, "r60"].sum(), -x.loc[x.r60 < 0, "r60"].sum()
        return pd.Series({"n": len(x), "30分%": x.r30.mean() * 100, "60分%": x.r60.mean() * 100,
                          "勝率60": (x.r60 > 0).mean() * 100, "PF60": pos / neg if neg else np.nan,
                          "前半60%": x.loc[x.half == "H1", "r60"].mean() * 100,
                          "後半60%": x.loc[x.half == "H2", "r60"].mean() * 100})

    pd.set_option("display.width", 220)
    print(f"period {ev['datetime'].min().date()}..{ev['datetime'].max().date()}  (コスト 0.10% 込み)")
    print("\n=== 全体 ===")
    print(ev.groupby("side").apply(s, include_groups=False).round(3).to_string())
    b = ev[ev.side == "BUY"].copy()
    b["深さ"] = pd.cut(b["depth"], [-1, -0.006, -0.003, -0.001, 0], labels=["-0.6%未満", "-0.6〜-0.3%", "-0.3〜-0.1%", "-0.1〜0%"])
    print("\n=== 買い: MACD の深さ (MACD/終値) 別 ===")
    print(b.groupby("深さ", observed=True).apply(s, include_groups=False).round(3).to_string())
    print("\n=== 買い: 追加の条件 ===")
    conds = {
        "25MA より上": b["above25"] > 0,
        "75MA 上向き": b["slope75"] > 0,
        "75MA 上向き & 25MA より上": (b["slope75"] > 0) & (b["above25"] > 0),
        "当日 -2% 以上下げた後": b["from_open"] <= -0.02,
        "深さ -0.3% 未満 & 75MA 上向き": (b["depth"] < -0.003) & (b["slope75"] > 0),
    }
    print(pd.DataFrame({k: s(b[m]) for k, m in conds.items()}).T.round(3).to_string())


if __name__ == "__main__":
    main()
