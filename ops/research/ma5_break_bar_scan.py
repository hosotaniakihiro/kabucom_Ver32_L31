# ============================================================
# ops/research/ma5_break_bar_scan.py
# 2026-10-20: 5 分足の終値が 5 本移動平均を下に抜けてから「何本目」で売ると成績が良いか (研究用)。
#
# きっかけ: 4493 を 7% 下げ切った後 (RSI 22) に売って底で捕まった。
#   ユーザー: 「5MA をした抜けして何本目のエントリーしていて何本目が勝率がいいか検証して」
#
# n 本目 = 終値 < MA5 の足が続いた本数 (下抜けた足が 1 本目)。n 本目が確定した次の足の始値で売る。
# 出口: 30 分後の終値 / 60 分後の終値 / 終値が MA5 以上に戻った足の終値 (戻らなければ 15:20)。
# 往復コスト 0.10% (デイトレ空売りのプレミアム料は含まない)。参考に上抜けの買いも同じ形で出す。
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
MAX_N = 10


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
    b = b.merge(keep, on=["symbol", "d"])
    return b.sort_values(["symbol", "datetime"]).reset_index(drop=True)


def events(b: pd.DataFrame, side: str) -> pd.DataFrame:
    g = b.groupby("symbol", sort=False)
    b = b.copy()
    b["ma5"] = g["close"].transform(lambda s: s.rolling(5).mean())
    b["ma25"] = g["close"].transform(lambda s: s.rolling(25).mean())
    delta = g["close"].diff()
    up = delta.clip(lower=0).groupby(b["symbol"]).transform(lambda s: s.ewm(alpha=1 / 14, adjust=False).mean())
    dn = (-delta.clip(upper=0)).groupby(b["symbol"]).transform(lambda s: s.ewm(alpha=1 / 14, adjust=False).mean())
    b["rsi"] = 100 - 100 / (1 + up / dn.replace(0, np.nan))
    sgn = -1 if side == "SELL" else 1
    on = (b["close"] < b["ma5"]) if side == "SELL" else (b["close"] > b["ma5"])
    on &= b["ma5"].notna()
    # 同じ日の中で on が続いた本数
    key = b["symbol"] + b["d"].astype(str)
    brk = (~on) | (key != key.shift(1))
    run_id = brk.cumsum()
    b["n"] = np.where(on, on.groupby(run_id).cumsum(), 0)
    sd = [b["symbol"], b["d"]]
    gd = b.groupby(sd, sort=False)
    b["entry"] = gd["open"].shift(-1)
    b["entry_t"] = gd["datetime"].shift(-1)
    b["c6"] = gd["close"].shift(-6)
    b["c12"] = gd["close"].shift(-12)
    # MA5 の反対側に戻った最初の足の終値 (n 本目の次の足以降)。戻らなければ 15:20 の足
    b["is_last"] = b["datetime"].dt.time <= dt.time(15, 20)
    last_c = b[b["is_last"]].groupby(sd)["close"].last().rename("last_c")
    b = b.join(last_c, on=["symbol", "d"])
    back = (~on) & b["ma5"].notna() & b["is_last"]
    idx = pd.Series(np.where(back, np.arange(len(b)), np.nan), index=b.index)
    # 次の戻り足の位置: 後ろから埋める (同じ銘柄・日の中だけ)
    pos = idx[::-1].groupby((key[::-1] != key[::-1].shift(1)).cumsum()).ffill()[::-1]
    pos_after = pos.groupby(key).shift(-1)  # 自分より後
    b["c_back"] = np.where(pos_after.notna(), b["close"].to_numpy()[pos_after.fillna(0).astype(int)], b["last_c"])
    tm = b["entry_t"].dt.time
    ev = b[(b["n"] >= 1) & (b["n"] <= MAX_N) & b["entry"].notna() & (tm >= dt.time(9, 15)) & (tm <= dt.time(14, 30))].copy()
    for k in ("c6", "c12", "c_back"):
        ev[k] = ev[k].where(ev[k].notna(), ev["last_c"])
        ev["r_" + k] = (ev[k] / ev["entry"] - 1) * sgn - COST
    ev["day_chg"] = ev["close"] / ev["prev_c"] - 1
    ev["half"] = np.where(ev["d"] < ev["d"].quantile(0.5), "前半", "後半")
    return ev


def table(ev: pd.DataFrame, by: str) -> pd.DataFrame:
    def s(x: pd.DataFrame) -> pd.Series:
        return pd.Series({
            "件数": len(x),
            "勝率30分": (x["r_c6"] > 0).mean() * 100,
            "30分%": x["r_c6"].mean() * 100,
            "60分%": x["r_c12"].mean() * 100,
            "戻りまで%": x["r_c_back"].mean() * 100,
            "勝率戻り": (x["r_c_back"] > 0).mean() * 100,
            "前半30分%": x.loc[x["half"] == "前半", "r_c6"].mean() * 100,
            "後半30分%": x.loc[x["half"] == "後半", "r_c6"].mean() * 100,
        })
    return ev.groupby(by, observed=True).apply(s, include_groups=False).round(3)


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    pd.set_option("display.width", 220)
    b = load()
    print(f"期間 {b['d'].min().date()}..{b['d'].max().date()}  銘柄日 {b.groupby(['symbol','d']).ngroups:,}  コスト 0.10% 込み", flush=True)
    for side, name in (("SELL", "下抜けで売り"), ("BUY", "上抜けで買い (参考)")):
        ev = events(b, side)
        ev.to_pickle(os.path.join(LOCAL, "research", f"ma5_break_{side.lower()}.pkl"))
        print(f"\n===== {name}: 全体 =====")
        print(table(ev, "n").to_string())
        if side == "SELL":
            mv = ev[ev["day_chg"] <= -0.02]
            print(f"\n===== {name}: 当日 -2% 以上下げている銘柄だけ (システムの売り候補に近い) =====")
            print(table(mv, "n").to_string())
            ev["rsi_bin"] = pd.cut(ev["rsi"], [0, 30, 40, 50, 100], labels=["RSI<30", "30-40", "40-50", "50+"])
            print(f"\n===== {name}: 1〜3 本目を RSI 別に =====")
            print(table(ev[ev["n"] <= 3], ["n", "rsi_bin"]).to_string())
            ev["ma25_side"] = np.where(ev["close"] < ev["ma25"], "25MA の下", "25MA の上")
            print(f"\n===== {name}: 1〜3 本目を 25MA の上下別に =====")
            print(table(ev[ev["n"] <= 3], ["n", "ma25_side"]).to_string())


if __name__ == "__main__":
    main()
