# ============================================================
# ops/research/early_signal_scan.py
# 2026-10-19: 今のエントリーサイン (終値 > 5MA > 25MA・RSI 50〜80・MACD > signal) より
#   早く入るサインを 5 分足で比べる (研究用、本番には入れない)。
#
# ユーザー指摘: 「上昇してから買い、下落してから売りなので利益が残らない。
#   今のサインが出る前に入りたい。移動平均の向きだけでなく差を見るなど」
#
# サイン (買い。売りは符号を反転):
#   BASE      : 今のゲート相当の条件が初めて成立した足
#   GAP_TURN  : 5MA-25MA の差 (終値比) がまだマイナスで、2 本続けて縮小 + 終値 > 5MA (ゴールデンクロス前)
#   GAP_CROSS : 差がマイナスからプラスに変わった足 (クロスそのもの)
#   HIST_TURN : MACD ヒストグラムがマイナスで 2 本続けて増加 (MACD のクロス前)
#   MA5_TURN  : 5MA の傾きがマイナスからプラスに変わり、終値 > 5MA
# 評価: 次の足の始値で入り、3 本 (15 分) / 6 本 (30 分) 後の終値で出る。往復コスト 0.10% を引く。
#   同じ銘柄・同じサインは 6 本 (30 分) に 1 回まで。9:30〜14:00 のサインだけ。
#   流動性: 手前 12 本の 5 分売買代金の平均が 500 万円以上。
#
# 使い方: python ops/research/early_signal_scan.py  (bars5m.pkl は ma75_turnaround_scan.py load で作る)
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
MIN_TURNOVER = 5_000_000
COOLDOWN = 6


def _rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def _features(g: pd.DataFrame) -> pd.DataFrame:
    c = g["close"]
    g = g.copy()
    g["ma5"] = c.rolling(5, min_periods=5).mean()
    g["ma25"] = c.rolling(25, min_periods=25).mean()
    g["gap"] = (g["ma5"] - g["ma25"]) / c
    g["rsi"] = _rsi(c)
    ema12, ema26 = c.ewm(span=12, adjust=False).mean(), c.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    sig = macd.ewm(span=9, adjust=False).mean()
    g["hist"] = macd - sig
    g["turnover"] = (c * g["volume"]).rolling(12, min_periods=12).mean().shift(1)
    g["volr"] = g["volume"] / g["volume"].rolling(12, min_periods=12).mean().shift(1)
    g["ma75"] = c.rolling(75, min_periods=75).mean()
    g["ma75_slope"] = g["ma75"] / g["ma75"].shift(6) - 1
    g["gapabs"] = g["gap"].abs()
    g["nxt_open"] = g["open"].shift(-1)
    g["c3"] = c.shift(-3)
    g["c6"] = c.shift(-6)
    g["day"] = g["datetime"].dt.normalize()
    g["day_open"] = g.groupby("day")["open"].transform("first")
    g["from_open"] = c / g["day_open"] - 1
    # 先の足が同じ日でなければ評価しない (持ち越さない)
    g["ok3"] = g["day"].shift(-3) == g["day"]
    g["ok6"] = g["day"].shift(-6) == g["day"]
    return g


def _signals(g: pd.DataFrame) -> dict[str, tuple[pd.Series, pd.Series]]:
    c, ma5, ma25, gap, hist, rsi = g["close"], g["ma5"], g["ma25"], g["gap"], g["hist"], g["rsi"]
    macd_up = hist > 0
    base_b = (c > ma5) & (ma5 > ma25) & rsi.between(50, 80) & macd_up
    base_s = (c < ma5) & (ma5 < ma25) & rsi.between(20, 50) & ~macd_up
    g1, g2 = gap.shift(1), gap.shift(2)
    h1, h2 = hist.shift(1), hist.shift(2)
    m1, m2 = ma5.shift(1), ma5.shift(2)
    return {
        "BASE": (base_b & ~base_b.shift(1, fill_value=False), base_s & ~base_s.shift(1, fill_value=False)),
        "GAP_TURN": ((gap < 0) & (gap > g1) & (g1 > g2) & (c > ma5), (gap > 0) & (gap < g1) & (g1 < g2) & (c < ma5)),
        "GAP_CROSS": ((gap > 0) & (g1 <= 0), (gap < 0) & (g1 >= 0)),
        "HIST_TURN": ((hist < 0) & (hist > h1) & (h1 > h2), (hist > 0) & (hist < h1) & (h1 < h2)),
        "MA5_TURN": ((ma5 > m1) & (m1 <= m2) & (c > ma5), (ma5 < m1) & (m1 >= m2) & (c < ma5)),
    }


def _cooldown(idx: np.ndarray) -> np.ndarray:
    keep, last = [], -10**9
    for i in idx:
        if i - last >= COOLDOWN:
            keep.append(i)
            last = i
    return np.array(keep, dtype=int)


def main() -> None:
    bars = pd.read_pickle(CACHE)
    bars = bars.sort_values(["symbol", "datetime"]).reset_index(drop=True)
    days = bars.groupby(bars["datetime"].dt.normalize())["symbol"].nunique()
    good_days = set(days[days >= 100].index)
    bars = bars[bars["datetime"].dt.normalize().isin(good_days)]
    print(f"bars={len(bars)} symbols={bars['symbol'].nunique()} days={len(good_days)}", flush=True)

    rows = []
    t_lo, t_hi = dt.time(9, 30), dt.time(14, 0)
    for n, (sym, g) in enumerate(bars.groupby("symbol", sort=False)):
        if len(g) < 60:
            continue
        g = _features(g.reset_index(drop=True))
        tm = g["datetime"].dt.time
        base_ok = (tm >= t_lo) & (tm <= t_hi) & (g["turnover"] >= MIN_TURNOVER) & g["nxt_open"].gt(0) & g["ok6"]
        for name, (sb, ss) in _signals(g).items():
            for side, mask in (("BUY", sb), ("SELL", ss)):
                idx = _cooldown(np.flatnonzero((mask & base_ok).to_numpy()))
                if len(idx) == 0:
                    continue
                e = g.loc[idx]
                sgn = 1.0 if side == "BUY" else -1.0
                rows.append(pd.DataFrame({
                    "sig": name, "side": side, "symbol": sym, "datetime": e["datetime"].to_numpy(),
                    "r15": sgn * (e["c3"].to_numpy() / e["nxt_open"].to_numpy() - 1) - COST,
                    "r30": sgn * (e["c6"].to_numpy() / e["nxt_open"].to_numpy() - 1) - COST,
                    "volr": e["volr"].to_numpy(), "trend": sgn * e["ma75_slope"].to_numpy(),
                    "gapabs": e["gapabs"].to_numpy(), "from_open": sgn * e["from_open"].to_numpy(),
                    "above75": sgn * (e["close"].to_numpy() / e["ma75"].to_numpy() - 1),
                }))
        if n % 500 == 0:
            print(f"  {n} symbols", flush=True)
    ev = pd.concat(rows, ignore_index=True).dropna()
    # 外れ値 (データ破損) を落とす
    ev = ev[(ev["r15"].abs() < 0.2) & (ev["r30"].abs() < 0.2)]
    mid = ev["datetime"].quantile(0.5)
    ev["half"] = np.where(ev["datetime"] < mid, "H1", "H2")

    def summ(x: pd.DataFrame) -> pd.Series:
        pos, neg = x.loc[x["r30"] > 0, "r30"].sum(), -x.loc[x["r30"] < 0, "r30"].sum()
        return pd.Series({
            "n": len(x), "avg15%": x["r15"].mean() * 100, "avg30%": x["r30"].mean() * 100,
            "win30": (x["r30"] > 0).mean() * 100, "pf30": pos / neg if neg > 0 else np.nan,
        })

    pd.set_option("display.width", 200)
    print("\n=== 全期間 (コスト 0.10% 込み、30 分後) ===")
    print(ev.groupby(["sig", "side"]).apply(summ).round(3).to_string())
    print("\n=== 前半 / 後半 ===")
    print(ev.groupby(["sig", "side", "half"]).apply(summ)[["n", "avg30%", "win30"]].round(3).unstack("half").to_string())
    out = os.path.join(LOCAL, "research", "early_signal_events.pkl")
    ev.to_pickle(out)
    print(f"\nsaved {out} rows={len(ev)} period={ev['datetime'].min()}..{ev['datetime'].max()}")


if __name__ == "__main__":
    sys.exit(main())
