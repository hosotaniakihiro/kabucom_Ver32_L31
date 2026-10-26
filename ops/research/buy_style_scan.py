# ============================================================
# ops/research/buy_style_scan.py
# 2026-10-19: 買いを「順張り」と「逆張り」に分けたらどうなるか (研究用、本番には入れない)。
#
# ユーザー質問: 「買いを順張りか逆張りかに明確に決めたらどうなりますか」
#   (過去 18 日の実売買で、買い 215 回 -62,140 円 / 売り 32 回 +45,240 円 と分かった後)
#
# 5 分足 (bars5m.pkl)。次の足の始値で買い、30 分 / 60 分後の終値で売る。往復コスト 0.10%。
#   順張り: 直近 30 分の上昇 r30 >= X、または 25MA から +X% 上
#   逆張り: 直近 30 分の下落 r30 <= -X、または 25MA から -X% 下、RSI <= X
#   同じ銘柄は 12 本 (60 分) に 1 回まで、9:30〜14:00、手前 12 本の 5 分売買代金平均 500 万円以上。
# ============================================================
from __future__ import annotations

import datetime as dt
import os

import numpy as np
import pandas as pd

LOCAL = os.path.join(os.getenv("LOCALAPPDATA") or r"C:\Temp", "autostock")
CACHE = os.path.join(LOCAL, "research", "ma75_turnaround", "bars5m.pkl")
COST = 0.0010
COOLDOWN = 12


def _rsi(c: pd.Series, n: int = 14) -> pd.Series:
    d = c.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def _cooldown(idx: np.ndarray) -> np.ndarray:
    keep, last = [], -10**9
    for i in idx:
        if i - last >= COOLDOWN:
            keep.append(i)
            last = i
    return np.array(keep, dtype=int)


def main() -> None:
    bars = pd.read_pickle(CACHE).sort_values(["symbol", "datetime"]).reset_index(drop=True)
    days = bars.groupby(bars["datetime"].dt.normalize())["symbol"].nunique()
    bars = bars[bars["datetime"].dt.normalize().isin(set(days[days >= 100].index))]
    t_lo, t_hi = dt.time(9, 30), dt.time(14, 0)
    rules = {
        "順張り r30>=+1%": lambda g: g["r30"] >= 0.01,
        "順張り r30>=+2%": lambda g: g["r30"] >= 0.02,
        "順張り r30>=+3%": lambda g: g["r30"] >= 0.03,
        "順張り 25MA+2%": lambda g: g["dev25"] >= 0.02,
        "順張り 当日高値更新": lambda g: g["new_high"],
        "逆張り r30<=-1%": lambda g: g["r30"] <= -0.01,
        "逆張り r30<=-2%": lambda g: g["r30"] <= -0.02,
        "逆張り r30<=-3%": lambda g: g["r30"] <= -0.03,
        "逆張り 25MA-2%": lambda g: g["dev25"] <= -0.02,
        "逆張り 25MA-3%": lambda g: g["dev25"] <= -0.03,
        "逆張り RSI<=25": lambda g: g["rsi"] <= 25,
        "逆張り RSI<=20": lambda g: g["rsi"] <= 20,
        "逆張り 当日-5%&r30<=-2%": lambda g: (g["from_open"] <= -0.05) & (g["r30"] <= -0.02),
    }
    out = []
    for sym, g in bars.groupby("symbol", sort=False):
        if len(g) < 80:
            continue
        g = g.reset_index(drop=True)
        c = g["close"]
        day = g["datetime"].dt.normalize()
        g["r30"] = c / c.shift(6) - 1
        g.loc[day != day.shift(6), "r30"] = np.nan          # 前日をまたぐ変化は使わない
        g["dev25"] = c / c.rolling(25, min_periods=25).mean() - 1
        g["rsi"] = _rsi(c)
        g["from_open"] = c / g.groupby(day)["open"].transform("first") - 1
        g["new_high"] = g["high"] >= g.groupby(day)["high"].cummax().shift(1).where(day == day.shift(1)) * 1.0
        g["new_high"] = g["new_high"].fillna(False) & (g["from_open"] > 0.01)
        turnover = (c * g["volume"]).rolling(12, min_periods=12).mean().shift(1)
        nxt = g["open"].shift(-1)
        r30f = c.shift(-6) / nxt - 1 - COST
        r60f = c.shift(-12) / nxt - 1 - COST
        ok6, ok12 = day.shift(-6) == day, day.shift(-12) == day
        tm = g["datetime"].dt.time
        base = (tm >= t_lo) & (tm <= t_hi) & (turnover >= 5_000_000) & (nxt > 0) & ok12
        for name, f in rules.items():
            idx = _cooldown(np.flatnonzero((f(g).fillna(False) & base).to_numpy()))
            if len(idx):
                out.append(pd.DataFrame({"rule": name, "datetime": g["datetime"].to_numpy()[idx],
                                         "r30": r30f.to_numpy()[idx], "r60": np.where(ok12.to_numpy()[idx], r60f.to_numpy()[idx], np.nan)}))
    ev = pd.concat(out, ignore_index=True).dropna()
    ev = ev[(ev["r30"].abs() < 0.3) & (ev["r60"].abs() < 0.3)]
    mid = ev["datetime"].quantile(0.5)
    ev["half"] = np.where(ev["datetime"] < mid, "H1", "H2")
    ev["month"] = ev["datetime"].dt.strftime("%m")

    def s(x: pd.DataFrame) -> pd.Series:
        pos, neg = x.loc[x.r60 > 0, "r60"].sum(), -x.loc[x.r60 < 0, "r60"].sum()
        return pd.Series({"n": len(x), "30分%": x.r30.mean() * 100, "60分%": x.r60.mean() * 100,
                          "勝率60": (x.r60 > 0).mean() * 100, "PF60": pos / neg if neg else np.nan,
                          "前半60%": x.loc[x.half == "H1", "r60"].mean() * 100,
                          "後半60%": x.loc[x.half == "H2", "r60"].mean() * 100})
    pd.set_option("display.width", 220)
    order = list(rules)
    res = ev.groupby("rule").apply(s, include_groups=False).reindex(order)
    print(f"period {ev['datetime'].min().date()}..{ev['datetime'].max().date()}  (コスト 0.10% 込み)")
    print(res.round(3).to_string())
    print("\n=== 月別 60分% ===")
    print(ev.pivot_table(index="rule", columns="month", values="r60", aggfunc="mean").reindex(order).mul(100).round(3).to_string())


if __name__ == "__main__":
    main()
