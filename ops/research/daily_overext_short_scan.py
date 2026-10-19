# ============================================================
# ops/research/daily_overext_short_scan.py
# 2026-10-19: 日足で 25 日平均から大きく上に離れた銘柄が、当日崩れたところを売る (研究用)。
#
# きっかけ: 4382 (10/02 855 円 -> 10/16 1,754 円、25 日平均 +81%) が 10/19 に高値から -19%。
#   ユーザー: 「この銘柄は最近急騰していて日足の移動平均との乖離が大きいです」
#
# 5 分足 (bars5m.pkl) から日足を作る。前日終値の 25 日平均乖離 dev25 が X% 以上の銘柄で、
# 当日の 5 分足終値が「当日高値から -Y%」を初めて割った足の次の足の始値で売る (1 日 1 回)。
# 出口: (a) 当日 15:00 の終値 / (b) 当日大引け / (c) 翌日寄り / (d) 翌日大引け。
#   損切り: 売値から +5% (5 分足の高値で判定、当日中のみ)。往復コスト 0.15% (急騰銘柄はスプレッドが広い)。
# ============================================================
from __future__ import annotations

import datetime as dt
import os

import numpy as np
import pandas as pd

LOCAL = os.path.join(os.getenv("LOCALAPPDATA") or r"C:\Temp", "autostock")
CACHE = os.path.join(LOCAL, "research", "ma75_turnaround", "bars5m.pkl")
COST = 0.0015
STOP = 0.05


def main() -> None:
    bars = pd.read_pickle(CACHE).sort_values(["symbol", "datetime"]).reset_index(drop=True)
    bars["d"] = bars["datetime"].dt.normalize()
    days = bars.groupby("d")["symbol"].nunique()
    good = set(days[days >= 100].index)
    bars = bars[bars["d"].isin(good)]
    daily = bars.groupby(["symbol", "d"]).agg(o=("open", "first"), h=("high", "max"), c=("close", "last"),
                                               v=("volume", "sum")).reset_index()
    daily["ma25"] = daily.groupby("symbol")["c"].transform(lambda s: s.rolling(25, min_periods=20).mean())
    daily["dev25_prev"] = (daily["c"] / daily["ma25"] - 1).groupby(daily["symbol"]).shift(1)
    daily["prev_c"] = daily.groupby("symbol")["c"].shift(1)
    daily["nxt_o"] = daily.groupby("symbol")["o"].shift(-1)
    daily["nxt_c"] = daily.groupby("symbol")["c"].shift(-1)
    daily["turn_prev"] = (daily["c"] * daily["v"]).groupby(daily["symbol"]).shift(1)
    cand = daily[(daily["dev25_prev"] >= 0.2) & (daily["turn_prev"] >= 3e8)]
    print(f"候補日 (前日 25 日平均 +20% 以上、前日売買代金 3 億円以上): {len(cand)} 銘柄日", flush=True)

    key = set(zip(cand["symbol"], cand["d"]))
    sub = bars[[k in key for k in zip(bars["symbol"], bars["d"])]]
    info = cand.set_index(["symbol", "d"])
    rows = []
    for (sym, d), g in sub.groupby(["symbol", "d"]):
        g = g.reset_index(drop=True)
        r = info.loc[(sym, d)]
        hi = g["high"].cummax()
        tm = g["datetime"].dt.time
        for y in (0.05, 0.08, 0.12):
            hit = np.flatnonzero(((g["close"] <= hi * (1 - y)) & (tm >= dt.time(9, 30)) & (tm <= dt.time(14, 30))).to_numpy())
            if not len(hit) or hit[0] + 1 >= len(g):
                continue
            i = hit[0] + 1
            entry = g.at[i, "open"]
            after = g.iloc[i:]
            stop_px = entry * (1 + STOP)
            stopped = (after["high"] >= stop_px).any()
            c1500 = after.loc[after["datetime"].dt.time <= dt.time(15, 0), "close"]
            x = {"a_1500": c1500.iloc[-1] if len(c1500) else np.nan, "b_close": r["c"], "c_nxt_open": r["nxt_o"], "d_nxt_close": r["nxt_c"]}
            row = {"symbol": sym, "d": d, "drop": y, "dev25": r["dev25_prev"], "entry_time": g.at[i, "datetime"].time()}
            for k, px in x.items():
                ret = entry / px - 1 - COST if px and px > 0 else np.nan
                if k in ("a_1500", "b_close") and stopped:
                    ret = -STOP - COST
                row[k] = ret
            rows.append(row)
    ev = pd.DataFrame(rows)
    ev["dev_bin"] = pd.cut(ev["dev25"], [0.2, 0.3, 0.5, 10], labels=["+20〜30%", "+30〜50%", "+50%以上"])
    ev["half"] = np.where(ev["d"] < ev["d"].quantile(0.5), "H1", "H2")
    ev.to_pickle(os.path.join(LOCAL, "research", "overext_short_events.pkl"))
    pd.set_option("display.width", 220)

    def s(x: pd.DataFrame) -> pd.Series:
        o = {"n": len(x), "銘柄": x["symbol"].nunique()}
        for k, nm in (("a_1500", "15時"), ("b_close", "大引け"), ("c_nxt_open", "翌寄り"), ("d_nxt_close", "翌引け")):
            v = x[k].dropna()
            o[f"{nm}%"] = v.mean() * 100
            o[f"{nm}勝率"] = (v > 0).mean() * 100
        o["翌引け前半%"] = x.loc[x.half == "H1", "d_nxt_close"].mean() * 100
        o["翌引け後半%"] = x.loc[x.half == "H2", "d_nxt_close"].mean() * 100
        return pd.Series(o)

    print(f"期間 {ev['d'].min().date()}..{ev['d'].max().date()}  売り、コスト 0.15% 込み、当日中は +5% 損切り")
    print(ev.groupby(["drop", "dev_bin"], observed=True).apply(s, include_groups=False).round(2).to_string())
    print("\n4382 のような例 (dev25 +50% 以上、高値 -8%) の明細:")
    print(ev[(ev["dev25"] >= 0.5) & (ev["drop"] == 0.08)][["symbol", "d", "entry_time", "dev25", "a_1500", "b_close", "c_nxt_open", "d_nxt_close"]].round(3).to_string())


if __name__ == "__main__":
    main()
