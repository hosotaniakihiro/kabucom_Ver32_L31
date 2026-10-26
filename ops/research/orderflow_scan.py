# ============================================================
# ops/research/orderflow_scan.py
# 2026-10-19: 板の偏りと歩み値の勢いで、数分先の値動きを当てられるか (研究用、本番には入れない)。
#
# ユーザー指示: 「板の偏りと歩み値の勢いで試して」
#   (early_signal_scan.py で 5 分足の MA/MACD 系は早めても遅くしてもコスト負けと分かった後)
#
# データ: %LOCALAPPDATA%\autostock\push\pushYYYYMMDD.db の stream_data_raw (PUSH のティック、10 本板つき)。
# 特徴量 (銘柄ごと、30 秒刻みのスナップショット):
#   imb5  : 上位 5 本の (買い板株数 - 売り板株数) / 合計    (+ = 買い板が厚い)
#   imb1  : 最良気配 1 本だけの同じ値
#   flow60: 直近 60 秒の (買い約定 - 売り約定) / 約定合計。売り気配以上の約定 = 買い、買い気配以下 = 売り
#   inten : 直近 60 秒の出来高 / その日の 60 秒あたり平均出来高
#   mom60 : 直近 60 秒の仲値の変化率
# 評価:
#   mid1/3/5 : 1/3/5 分後の仲値の変化率 (コスト前)
#   ex3/5    : 買い = 今の売り気配で買い、3/5 分後の買い気配で売る (売りは逆)。スプレッド込みの実額
#
# 使い方: python ops/research/orderflow_scan.py build   (日ごとに特徴量を作って保存)
#         python ops/research/orderflow_scan.py report
# ============================================================
from __future__ import annotations

import glob
import os
import re
import sqlite3
import sys
import time

import numpy as np
import pandas as pd

LOCAL = os.path.join(os.getenv("LOCALAPPDATA") or r"C:\Temp", "autostock")
PUSH_DIR = os.path.join(LOCAL, "push")
OUT_DIR = os.path.join(LOCAL, "research", "orderflow")
STEP = "30s"
COLS = (["symbol", "received_at", "price", "volume"]
        + [f"Buy{i}Qty" for i in range(1, 6)] + [f"Sell{i}Qty" for i in range(1, 6)]
        + ["Buy1Price", "Sell1Price"])


def _load_day(path: str) -> pd.DataFrame:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    try:
        q = f"SELECT {', '.join(COLS)} FROM stream_data_raw WHERE time BETWEEN '09:00:00' AND '15:00:00'"
        df = pd.read_sql_query(q, con)
    finally:
        con.close()
    df["ts"] = pd.to_datetime(df["received_at"], errors="coerce")
    df = df.drop(columns=["received_at"]).dropna(subset=["ts"])
    for c in COLS[2:]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    # kabu の Buy1/Sell1: Sell = 売り気配 (ask)、Buy = 買い気配 (bid)。クロスや欠損の行は使わない
    df = df[(df["Buy1Price"] > 0) & (df["Sell1Price"] > df["Buy1Price"]) & (df["price"] > 0)]
    return df.sort_values(["symbol", "ts"])


def _symbol_features(g: pd.DataFrame) -> pd.DataFrame | None:
    g = g.drop_duplicates("ts", keep="last").set_index("ts")
    if len(g) < 200:
        return None
    bid, ask = g["Buy1Price"], g["Sell1Price"]
    mid = (bid + ask) / 2
    bq = g[[f"Buy{i}Qty" for i in range(1, 6)]].sum(axis=1)
    sq = g[[f"Sell{i}Qty" for i in range(1, 6)]].sum(axis=1)
    # 約定: 累積出来高の増分。直前の気配に対して約定値で買い/売りを分ける
    dv = g["volume"].diff().clip(lower=0).fillna(0)
    pb, pa = bid.shift(1), ask.shift(1)
    buy_v = dv.where(g["price"] >= pa, 0.0)
    sell_v = dv.where(g["price"] <= pb, 0.0)
    t = pd.DataFrame({
        "bid": bid, "ask": ask, "mid": mid,
        "imb5": (bq - sq) / (bq + sq).replace(0, np.nan),
        "imb1": (g["Buy1Qty"] - g["Sell1Qty"]) / (g["Buy1Qty"] + g["Sell1Qty"]).replace(0, np.nan),
        "bv": buy_v.rolling("60s").sum(), "sv": sell_v.rolling("60s").sum(), "v60": dv.rolling("60s").sum(),
        "turn": (dv * g["price"]).rolling("60s").sum(),
    })
    # 30 秒刻みのスナップショット (直前の値。15 秒より古ければ捨てる)
    grid = pd.date_range(t.index.min().ceil(STEP), t.index.max().floor(STEP), freq=STEP)
    if len(grid) < 20:
        return None
    snap = pd.merge_asof(pd.DataFrame({"t": grid}), t.reset_index().rename(columns={"ts": "t_src"}),
                         left_on="t", right_on="t_src", direction="backward", tolerance=pd.Timedelta("15s"))
    snap = snap.dropna(subset=["mid"]).reset_index(drop=True)
    if snap.empty:
        return None
    snap["flow60"] = (snap["bv"] - snap["sv"]) / snap["v60"].replace(0, np.nan)
    day_avg = snap["v60"].mean()
    snap["inten"] = snap["v60"] / day_avg if day_avg > 0 else np.nan
    snap["spread"] = (snap["ask"] - snap["bid"]) / snap["mid"]
    # 先の値: 同じグリッドで k 本先 (データが途切れている所は時刻差で落とす)
    for k, name in ((2, "1"), (6, "3"), (10, "5")):
        fut = snap.shift(-k)
        ok = (fut["t"] - snap["t"]) == pd.Timedelta(seconds=30 * k)
        snap[f"mid{name}"] = np.where(ok, fut["mid"] / snap["mid"] - 1, np.nan)
        if name in ("3", "5"):
            snap[f"exb{name}"] = np.where(ok, fut["bid"] / snap["ask"] - 1, np.nan)   # 買い
            snap[f"exs{name}"] = np.where(ok, snap["bid"] / fut["ask"] - 1, np.nan)   # 売り (売って買い戻す)
    past = snap.shift(2)
    snap["mom60"] = np.where((snap["t"] - past["t"]) == pd.Timedelta("60s"), snap["mid"] / past["mid"] - 1, np.nan)
    return snap[["t", "imb5", "imb1", "flow60", "inten", "mom60", "spread", "turn",
                 "mid1", "mid3", "mid5", "exb3", "exb5", "exs3", "exs5"]]


def cmd_build() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    for p in sorted(glob.glob(os.path.join(PUSH_DIR, "push2*.db"))):
        ymd = re.search(r"push(\d{8})\.db$", p).group(1)
        out = os.path.join(OUT_DIR, f"feat_{ymd}.pkl")
        if os.path.exists(out) or os.path.getsize(p) < 50_000_000:
            continue
        t0 = time.time()
        try:
            df = _load_day(p)
        except Exception as e:
            print(f"{ymd} skip: {e}", flush=True)
            continue
        parts = []
        for sym, g in df.groupby("symbol", sort=False):
            f = _symbol_features(g)
            if f is not None:
                f.insert(0, "symbol", sym)
                parts.append(f)
        if not parts:
            continue
        feat = pd.concat(parts, ignore_index=True)
        feat.to_pickle(out)
        print(f"{ymd} ticks={len(df)} symbols={df['symbol'].nunique()} snaps={len(feat)} {time.time() - t0:.0f}s", flush=True)


def _summ(x: pd.DataFrame, side: str) -> pd.Series:
    ex = x[f"ex{'b' if side == 'BUY' else 's'}3"]
    ex5 = x[f"ex{'b' if side == 'BUY' else 's'}5"]
    sgn = 1 if side == "BUY" else -1
    return pd.Series({
        "n": len(x), "mid1%": sgn * x["mid1"].mean() * 100, "mid3%": sgn * x["mid3"].mean() * 100,
        "mid5%": sgn * x["mid5"].mean() * 100, "ex3%": ex.mean() * 100, "ex5%": ex5.mean() * 100,
        "win5": (ex5 > 0).mean() * 100, "days": x["day"].nunique(),
    })


def cmd_report() -> None:
    files = sorted(glob.glob(os.path.join(OUT_DIR, "feat_*.pkl")))
    ev = pd.concat([pd.read_pickle(f) for f in files], ignore_index=True)
    ev["day"] = ev["t"].dt.normalize()
    tm = ev["t"].dt.time
    ev = ev[(tm >= pd.Timestamp("09:05").time()) & (tm <= pd.Timestamp("14:50").time())
            & ~((tm > pd.Timestamp("11:28").time()) & (tm < pd.Timestamp("12:32").time()))]
    ev = ev[(ev["turn"] >= 1_000_000) & (ev["spread"] <= 0.003)]   # 60 秒で 100 万円以上、スプレッド 0.3% 以下
    days = sorted(ev["day"].unique())
    ev["half"] = np.where(ev["day"] < days[len(days) // 2], "H1", "H2")
    pd.set_option("display.width", 220)
    print(f"snaps={len(ev)} symbols={ev['symbol'].nunique()} days={len(days)} {days[0].date()}..{days[-1].date()}")
    print(f"平均スプレッド {ev['spread'].mean() * 100:.3f}%  ex は スプレッド込み (買い=売り気配で買って買い気配で売る)")

    for col, bins in (("imb5", [-1, -0.6, -0.3, -0.1, 0.1, 0.3, 0.6, 1]),
                      ("flow60", [-1, -0.6, -0.3, -0.1, 0.1, 0.3, 0.6, 1]),
                      ("inten", [0, 0.5, 1, 2, 4, 1e9]),
                      ("mom60", [-1, -0.005, -0.002, -0.0005, 0.0005, 0.002, 0.005, 1])):
        ev["b"] = pd.cut(ev[col], bins)
        print(f"\n=== {col} 別 (買いとして見た値: mid=コスト前、ex=スプレッド込み)")
        print(ev.groupby("b", observed=True).apply(lambda x: _summ(x, "BUY"), include_groups=False).round(3).to_string())

    print("\n=== 組み合わせ (買い: 板が買い厚 & 買い約定優勢 & 出来高増。売りは逆)")
    combos = {
        "imb>0.3 & flow>0.3": (ev["imb5"] > 0.3) & (ev["flow60"] > 0.3),
        "imb>0.3 & flow>0.3 & inten>2": (ev["imb5"] > 0.3) & (ev["flow60"] > 0.3) & (ev["inten"] > 2),
        "imb>0.6 & flow>0.6 & inten>2": (ev["imb5"] > 0.6) & (ev["flow60"] > 0.6) & (ev["inten"] > 2),
        "flow>0.6 & inten>4": (ev["flow60"] > 0.6) & (ev["inten"] > 4),
        "imb>0.3 & flow>0.3 & mom60<=0": (ev["imb5"] > 0.3) & (ev["flow60"] > 0.3) & (ev["mom60"] <= 0),
    }
    for name, m in combos.items():
        print(f"BUY  {name:32s}", _summ(ev[m], "BUY").round(3).to_dict())
    s_combos = {
        "imb<-0.3 & flow<-0.3": (ev["imb5"] < -0.3) & (ev["flow60"] < -0.3),
        "imb<-0.3 & flow<-0.3 & inten>2": (ev["imb5"] < -0.3) & (ev["flow60"] < -0.3) & (ev["inten"] > 2),
        "imb<-0.6 & flow<-0.6 & inten>2": (ev["imb5"] < -0.6) & (ev["flow60"] < -0.6) & (ev["inten"] > 2),
        "flow<-0.6 & inten>4": (ev["flow60"] < -0.6) & (ev["inten"] > 4),
    }
    for name, m in s_combos.items():
        print(f"SELL {name:32s}", _summ(ev[m], "SELL").round(3).to_dict())
    print("\n=== 前半/後半 (ex5%)")
    for name, m in {**combos}.items():
        print(f"BUY  {name:32s}", {h: round(_summ(ev[m & (ev['half'] == h)], 'BUY')['ex5%'], 3) for h in ('H1', 'H2')},
              {h: round(_summ(ev[m & (ev['half'] == h)], 'BUY')['mid5%'], 3) for h in ('H1', 'H2')})
    for name, m in s_combos.items():
        print(f"SELL {name:32s}", {h: round(_summ(ev[m & (ev['half'] == h)], 'SELL')['ex5%'], 3) for h in ('H1', 'H2')},
              {h: round(_summ(ev[m & (ev['half'] == h)], 'SELL')['mid5%'], 3) for h in ('H1', 'H2')})


if __name__ == "__main__":
    {"build": cmd_build, "report": cmd_report}[sys.argv[1] if len(sys.argv) > 1 else "report"]()
