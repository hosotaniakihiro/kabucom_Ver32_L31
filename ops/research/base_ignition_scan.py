# ============================================================
# ops/research/base_ignition_scan.py
# 2026-10-20: 「下げきって出来高が細り横ばい -> 号砲 (出来高急増) で上に抜ける」を買う (研究用)。
#
# きっかけ: 593A 10/20 10:54 安値 2,021 -> 11:04〜11:10 出来高 700〜2,300 株で横ばい ->
#   11:13〜11:15 出来高 11,000〜26,200 株で 2,045 -> 11:25 2,058。
#   ユーザー: 「下がりきったところで出来高が減り株価が横横になり、誰かが号砲を出して一気に買いが入った」
#
# 5 分足 (bars5m.pkl) で:
#   下げ   : 直近 DROP_LOOKBACK 本の高値から、土台の安値まで -DROP% 以上
#   土台   : 直前 BASE 本の値幅 (最高値/最安値-1) が BASE_RANGE% 以下、平均出来高が前の BASE 本の DRY 倍以下
#   号砲   : この足の出来高が土台の平均の IGN 倍以上、終値が土台の最高値を上に抜け、陽線
#   次の足の始値で買う。出口 30 分 / 60 分 / 土台の安値割れで損切り (それまで 60 分)。往復コスト 0.10%。
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
    return b.merge(keep, on=["symbol", "d"]).sort_values(["symbol", "datetime"]).reset_index(drop=True)


def scan(b: pd.DataFrame, *, base: int, base_range: float, dry: float, ign: float, drop: float, drop_lookback: int = 12) -> pd.DataFrame:
    sd = [b["symbol"], b["d"]]
    g = b.groupby(sd, sort=False)
    sh = lambda col, k: g[col].shift(k)
    # 土台 = この足の直前 base 本 (1..base 本前)
    base_hi = pd.concat([sh("high", k) for k in range(1, base + 1)], axis=1).max(axis=1)
    base_lo = pd.concat([sh("low", k) for k in range(1, base + 1)], axis=1).min(axis=1)
    base_vol = pd.concat([sh("volume", k) for k in range(1, base + 1)], axis=1).mean(axis=1)
    pre_vol = pd.concat([sh("volume", k) for k in range(base + 1, 2 * base + 1)], axis=1).mean(axis=1)
    pre_hi = pd.concat([sh("high", k) for k in range(base + 1, base + drop_lookback + 1)], axis=1).max(axis=1)
    cond = (
        (base_hi / base_lo - 1 <= base_range / 100)
        & (base_vol <= pre_vol * dry)
        & (b["volume"] >= base_vol * ign)
        & (b["close"] > base_hi)
        & (b["close"] > b["open"])
        & (base_lo / pre_hi - 1 <= -drop / 100)
        & (base_vol > 0)
    )
    b = b.assign(entry=g["open"].shift(-1), entry_t=g["datetime"].shift(-1), c6=g["close"].shift(-6), c12=g["close"].shift(-12),
                 base_lo=base_lo, base_hi=base_hi)
    tm = b["entry_t"].dt.time
    ev = b[cond & b["entry"].notna() & (tm >= dt.time(9, 15)) & (tm <= dt.time(14, 30))].copy()
    # 損切り: 60 分以内に安値が土台の安値を割ったら土台の安値で決済
    lows = pd.concat([g["low"].shift(-k) for k in range(1, 13)], axis=1).loc[ev.index]
    hit = lows.le(ev["base_lo"], axis=0)
    first = hit.idxmax(axis=1).where(hit.any(axis=1))
    last_c = b.groupby(sd)["close"].transform("last").loc[ev.index]
    for k in ("c6", "c12"):
        ev[k] = ev[k].fillna(last_c)
    ev["r30"] = ev["c6"] / ev["entry"] - 1 - COST
    ev["r60"] = ev["c12"] / ev["entry"] - 1 - COST
    ev["r60_stop"] = np.where(first.notna(), ev["base_lo"] / ev["entry"] - 1 - COST, ev["r60"])
    ev["stop_pct"] = (ev["base_lo"] / ev["entry"] - 1) * 100
    # 2026-10-20 (ユーザー): 時間ではなく 5MA で一旦利確。足の確定を待たず「株価が」5MA を割ったら売る。
    #   5MA は直前に確定した足までの値 (ma5 を 1 本ずらす)。足 k の安値がその線以下なら線の値段で、
    #   始値がすでに線より下なら始値で売る。同じ足で土台の安値も割ったら損切りを優先 (控えめ)。
    #   どちらも無ければその日の最後の終値。
    ma5 = g["close"].transform(lambda s: s.rolling(5).mean())
    H = 48
    sh_k = lambda ser, k: ser.groupby(sd).shift(-k)
    opens = pd.concat([g["open"].shift(-k) for k in range(1, H + 1)], axis=1).loc[ev.index].values
    lows48 = pd.concat([g["low"].shift(-k) for k in range(1, H + 1)], axis=1).loc[ev.index].values
    line = pd.concat([sh_k(ma5, k - 1) for k in range(1, H + 1)], axis=1).loc[ev.index].values
    nanc = np.isnan(lows48)
    hit_ma = (lows48 <= line) & ~nanc & ~np.isnan(line)
    hit_stop = (lows48 <= ev["base_lo"].values[:, None]) & ~nanc
    def first_idx(m):
        return np.where(m.any(axis=1), m.argmax(axis=1), 10**6)
    ib, istop = first_idx(hit_ma), first_idx(hit_stop)
    lastv = last_c.values
    out, bars = [], []
    for j in range(len(ev)):
        e = ev["entry"].values[j]
        if istop[j] < 10**6 and istop[j] <= ib[j]:
            px = min(opens[j, istop[j]], ev["base_lo"].values[j]); bars.append(istop[j] + 1)
        elif ib[j] < 10**6:
            px = min(opens[j, ib[j]], line[j, ib[j]]); bars.append(ib[j] + 1)
        else:
            px = lastv[j]; bars.append(np.nan)
        out.append(px / e - 1 - COST)
    ev["r_ma5"] = out
    ev["bars_ma5"] = bars
    ev["half"] = np.where(ev["d"] < b["d"].quantile(0.5), "前半", "後半")
    return ev


def summary(ev: pd.DataFrame) -> dict:
    return {"件数": len(ev), "銘柄": ev["symbol"].nunique(),
            "30分%": ev["r30"].mean() * 100, "勝率30": (ev["r30"] > 0).mean() * 100,
            "60分%": ev["r60"].mean() * 100, "60分損切り%": ev["r60_stop"].mean() * 100,
            "勝率60損切り": (ev["r60_stop"] > 0).mean() * 100,
            "前半60%": ev.loc[ev.half == "前半", "r60_stop"].mean() * 100,
            "後半60%": ev.loc[ev.half == "後半", "r60_stop"].mean() * 100,
            "損切り幅%": ev["stop_pct"].median(),
            "5MA利確%": ev["r_ma5"].mean() * 100, "勝率5MA": (ev["r_ma5"] > 0).mean() * 100,
            "5MA前半%": ev.loc[ev.half == "前半", "r_ma5"].mean() * 100, "5MA後半%": ev.loc[ev.half == "後半", "r_ma5"].mean() * 100,
            "保有本数": ev["bars_ma5"].median()}


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    pd.set_option("display.width", 220)
    b = load()
    print(f"期間 {b['d'].min().date()}..{b['d'].max().date()}  銘柄日 {b.groupby(['symbol','d']).ngroups:,}  コスト 0.10% 込み", flush=True)
    rows = []
    for base in (4, 6):
        for base_range in (0.6, 1.0):
            for dry in (0.5, 0.7):
                for ign in (3.0, 5.0):
                    for drop in (1.5, 3.0):
                        ev = scan(b, base=base, base_range=base_range, dry=dry, ign=ign, drop=drop)
                        rows.append({"土台本数": base, "土台値幅%": base_range, "出来高細り": dry, "号砲倍率": ign, "下げ%": drop, **summary(ev)})
                        print(rows[-1]["土台本数"], base_range, dry, ign, drop, rows[-1]["件数"], round(rows[-1]["5MA利確%"], 3), flush=True)
    t = pd.DataFrame(rows).round(3)
    t.to_csv(os.path.join(LOCAL, "research", "base_ignition_grid.csv"), index=False, encoding="utf-8-sig")
    print("\n===== 条件ごとの成績 (5MA 利確% の高い順) =====")
    cols = ["土台本数", "土台値幅%", "出来高細り", "号砲倍率", "下げ%", "件数", "5MA利確%", "勝率5MA", "5MA前半%", "5MA後半%",
            "保有本数", "60分損切り%", "損切り幅%"]
    print(t.sort_values("5MA利確%", ascending=False)[cols].to_string(index=False))


if __name__ == "__main__":
    main()
