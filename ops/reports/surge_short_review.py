# ============================================================
# ops/reports/surge_short_review.py
# 2026-10-06: 急騰空売り (SURGE_SHORT) の決済ルールを本番で検証する。
#   10/6 から 30 分 + トレール (含み益 1.0% から 0.5% 戻し) に変えた (ユーザー判断で本番検証)。
#   push DB の実気配 (9/7〜10/6、77 回) では 60 分・トレールなし -0.02% に対し -0.45% と出ていたので、
#   実際の取引ごとに「旧ルールならいくらだったか」を同じ気配で並べて比べる。
# 使い方: venv\Scripts\python.exe ops\reports\surge_short_review.py [YYYYMMDD ...]  (省略時は全日)
#   出力: %LOCALAPPDATA%\autostock\runtime\reports\surge_short_review.md
# 決済は売り気配 (Sell1Price) で買い戻した値で計算する。判定は 5 秒ごと (exit_loop の周期)。
# プレミアム料 (premium_pct) は差し引いた値も出す。
# ============================================================
import datetime as dt
import glob
import json
import os
import re
import sqlite3
import sys

import pandas as pd

LAD = os.path.expandvars(r"%LOCALAPPDATA%\autostock")
RULES = [  # (名前, 保有分, トレール開始%, 戻し%)
    ("現行 30分+トレール1.0/0.5", 30, 1.0, 0.5),
    ("旧 60分・トレールなし", 60, None, None),
    ("60分+トレール1.5/0.5", 60, 1.5, 0.5),
]
STOP_PCT, TP_PCT = 5.0, 5.0


def _ticks(day: str, symbol: str, start: dt.datetime) -> pd.DataFrame:
    p = os.path.join(LAD, "push", f"push{day}.db")
    if not os.path.exists(p):
        return pd.DataFrame()
    c = sqlite3.connect("file:" + p + "?mode=ro", uri=True, timeout=10)
    try:
        tabs = {r[0] for r in c.execute("select name from sqlite_master where type='table'")}
        tab = "stream_data" if "stream_data" in tabs else "stream_data_raw"
        t = pd.read_sql(f"select datetime, price, Sell1Price ask from {tab} where symbol=?", c, params=(symbol,))
    finally:
        c.close()
    t["datetime"] = pd.to_datetime(t.datetime.str[:19], errors="coerce")
    t = t.dropna(subset=["datetime"])
    t = t[(t.price > 0) & (t.ask > 0) & (t.datetime > start)]
    t = t[t.datetime <= start + dt.timedelta(minutes=61)]
    t = t[(t.datetime.dt.hour * 60 + t.datetime.dt.minute) < 15 * 60 + 20]
    return t.set_index("datetime").resample("5s").last().dropna()


def _sim(sub: pd.DataFrame, entry: float, start: dt.datetime, hold: int, a, b):
    best = entry
    last = None
    for ts, row in sub.iterrows():
        if ts > start + dt.timedelta(minutes=hold):
            break
        p, ask = float(row.price), float(row.ask)
        best = min(best, p)
        last = (ts, ask)
        if ask >= entry * (1 + STOP_PCT / 100):
            return (entry / ask - 1) * 100, ts, "STOP"
        if a is not None and best <= entry * (1 - a / 100) and p >= best * (1 + b / 100):
            return (entry / ask - 1) * 100, ts, "TRAIL"
        if p <= entry * (1 - TP_PCT / 100):
            return (entry / ask - 1) * 100, ts, "TAKE_PROFIT"
    if last is None:
        return None, None, "NO_DATA"
    return (entry / last[1] - 1) * 100, last[0], "TIME"


def main(days):
    files = sorted(glob.glob(os.path.join(LAD, "runtime", "surge_short_2026*.json")))
    rows = []
    for f in files:
        day = re.search(r"(\d{8})", f).group(1)
        if days and day not in days:
            continue
        st = json.load(open(f, encoding="utf-8"))
        for sym, t in (st.get("trades") or {}).items():
            if not t.get("closed"):
                continue
            entry = float(t.get("entry_fill") or t.get("price") or 0)
            if entry <= 0:
                continue
            start = dt.datetime.strptime(t["order_ts"], "%Y-%m-%d %H:%M:%S")
            prem = float(t.get("premium_pct") or 0)
            actual = (entry / float(t["exit_price"]) - 1) * 100 if t.get("exit_price") else None
            sub = _ticks(day, sym, start)
            r = {"day": day, "symbol": sym, "qty": int(t.get("qty") or 0), "entry": entry, "premium%": prem,
                 "actual%": actual, "actual_reason": t.get("exit_reason"),
                 "actual_min": round((dt.datetime.strptime(t["exit_ts"], "%Y-%m-%d %H:%M:%S") - start).total_seconds() / 60, 1) if t.get("exit_ts") else None}
            for name, hold, a, b in RULES:
                pnl, ts, why = _sim(sub, entry, start, hold, a, b)
                r[name] = pnl
                r[name + " 理由"] = why
                r[name + " 分"] = round((ts - start).total_seconds() / 60, 1) if ts is not None else None
            rows.append(r)
    out = os.path.join(LAD, "runtime", "reports", "surge_short_review.md")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    lines = ["# 急騰空売り 決済ルールの本番検証", "",
             f"- 作成 {dt.datetime.now():%Y-%m-%d %H:%M} / 取引 {len(rows)} 件",
             "- 損益% は建値比 (売り気配で買い戻し)。プレミアム料は別に差し引く", ""]
    if rows:
        df = pd.DataFrame(rows)
        lines += ["## 合計 (1 回あたり平均、プレミアム料控除後)", "", "| ルール | 平均% | 控除後% | 勝率 | 平均保有 |", "|---|---|---|---|---|"]
        a = df["actual%"].dropna()
        lines.append(f"| 実際の約定 | {a.mean():+.2f} | {(df['actual%'] - df['premium%']).mean():+.2f} | {(a > 0).mean() * 100:.0f}% | {df.actual_min.mean():.1f}分 |")
        for name, *_ in RULES:
            s = df[name].dropna()
            if len(s):
                lines.append(f"| {name} | {s.mean():+.2f} | {(df[name] - df['premium%']).mean():+.2f} | {(s > 0).mean() * 100:.0f}% | {df[name + ' 分'].mean():.1f}分 |")
        lines += ["", "## 取引ごと", "", "| 日 | 銘柄 | 株数 | 建値 | 実際 | " + " | ".join(n for n, *_ in RULES) + " |",
                  "|---|---|---|---|---|" + "---|" * len(RULES)]
        for _, r in df.iterrows():
            cells = [f"{r[n]:+.2f}% {r[n + ' 理由']} {r[n + ' 分']}分" if pd.notna(r[n]) else r[n + " 理由"] for n, *_ in RULES]
            act = f"{r['actual%']:+.2f}% {r.actual_reason} {r.actual_min}分" if pd.notna(r["actual%"]) else "-"
            lines.append(f"| {r.day} | {r.symbol} | {r.qty} | {r.entry:.1f} | {act} | " + " | ".join(cells) + " |")
    text = "\n".join(lines) + "\n"
    open(out, "w", encoding="utf-8").write(text)
    print(text)
    print("->", out)


if __name__ == "__main__":
    main(set(sys.argv[1:]))
