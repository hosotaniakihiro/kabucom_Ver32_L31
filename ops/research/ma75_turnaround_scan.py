"""
ops/research/ma75_turnaround_scan.py

5 分足 75MA の「下落 → 横ばい → 上向き + 上抜け定着 → 75MA への押し目」を過去データで探す。
2026-10-16: ユーザー要望 (対象 = 信用取引できる全銘柄、エントリー = 75MA まで押したところ)。
本番には組み込んでいない。まず当たり方と、その後の値動きを確認するための検証用。

データ: 日別 summary DB (summaryYYYYMMDD.db) の stock_summary_5min。夜間の Yahoo 取り込みで
        約 4,200 銘柄の 1 日分が入っている。NAS から読むと 1 日 6 秒かかるので、ローカルに pickle で持つ。

使い方:
  python ops/research/ma75_turnaround_scan.py load --days 60
  python ops/research/ma75_turnaround_scan.py scan
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import os
import re
import sqlite3
import sys
import time
from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from nas_root import nas_path  # noqa: E402

LOCAL = os.path.join(os.getenv("LOCALAPPDATA") or r"C:\Temp", "autostock")
OUT_DIR = os.path.join(LOCAL, "research", "ma75_turnaround")
CACHE = os.path.join(OUT_DIR, "bars5m.pkl")
MARGIN_CREDIT_TYPES = ("制度信用銘柄", "貸借銘柄")  # symbol_flags.is_margin は全行 0 なので使えない


@dataclass
class Params:
    ma: int = 75
    # ① 下落が続いていた: クロス直前 down_win 本の below_frac 以上で終値 < 75MA、
    #    75MA が下向きの本数が down_slope_frac 以上、75MA 自体が down_drop 以上下がっている
    down_win: int = 240          # 4 日
    below_frac: float = 0.80
    down_slope_frac: float = 0.65
    down_drop: float = -0.01
    # ③ 上抜け定着: クロス足を含む confirm_bars 本のうち confirm_min_above 本以上で終値 > 75MA
    confirm_bars: int = 6        # 30 分
    confirm_min_above: int = 5
    # ② 横ばい → 上向き (確認足の時点で判定)
    flat_win: int = 36           # 3 時間で見て
    flat_tol: float = 0.005      # 75MA の変化が ±0.5% 以内 (= 横ばい)
    up_win: int = 12             # 1 時間で見て
    # 押し目: 確認後 pullback_max 本以内に安値が 75MA * (1 + touch_band) まで来たら、その値で買い
    pullback_max: int = 120      # 2 日
    touch_band: float = 0.002
    # 「しばらく上で推移してから押してきた」: 押しの前に一度 75MA から away_min 以上離れていること。0 で無効
    away_min: float = 0.0
    # 評価: 損切り = 終値が 75MA * (1 - stop_band) を割る / 利確 = +take_profit / 時間切れ = hold_max 本
    stop_band: float = 0.005
    take_profit: float = 0.02
    hold_max: int = 60           # 1 日
    # 流動性: 押し目の手前 60 本の 5 分売買代金の平均 (円)。0 で無効
    min_turnover_5m: float = 5_000_000


def _day_files(days: int) -> list[tuple[str, str]]:
    d = nas_path(r"raw_data\kabu_station\summary")
    out = []
    for p in sorted(glob.glob(os.path.join(d, "summary2*.db"))):
        m = re.search(r"summary(\d{8})\.db$", p)
        if m:
            out.append((m.group(1), p))
    return out[-days:]


def cmd_load(days: int) -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    frames = []
    for ymd, p in _day_files(days):
        t0 = time.time()
        try:
            con = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=5.0)
            try:
                df = pd.read_sql_query(
                    "SELECT symbol, datetime, open, high, low, close, volume FROM stock_summary_5min", con
                )
            finally:
                con.close()
        except Exception as e:
            print(f"{ymd} skip: {e}")
            continue
        frames.append(df)
        print(f"{ymd} rows={len(df)} symbols={df['symbol'].nunique()} {time.time() - t0:.1f}s", flush=True)
    bars = pd.concat(frames, ignore_index=True)
    bars["symbol"] = bars["symbol"].astype(str).str.strip()
    bars["datetime"] = pd.to_datetime(bars["datetime"], errors="coerce")
    for k in ("open", "high", "low", "close", "volume"):
        bars[k] = pd.to_numeric(bars[k], errors="coerce")
    bars = bars.dropna(subset=["datetime", "close"])
    bars = bars[(bars["close"] > 0) & (bars["datetime"].dt.time >= dt.time(9, 0)) & (bars["datetime"].dt.time <= dt.time(15, 30))]
    # 休日のファイルには前営業日の分が入っている (summary20261012.db = 10/09) ので重複を落とす
    bars = bars.sort_values(["symbol", "datetime"]).drop_duplicates(["symbol", "datetime"], keep="last")
    bars.to_pickle(CACHE)
    print(f"saved {CACHE} rows={len(bars)} symbols={bars['symbol'].nunique()} "
          f"{bars['datetime'].min()} -> {bars['datetime'].max()}")


def _margin_symbols() -> set[str]:
    con = sqlite3.connect(f"file:{nas_path(r'Basic\symbol_flags.db')}?mode=ro", uri=True, timeout=5.0)
    try:
        rows = con.execute(
            f"SELECT symbol FROM symbol_flags WHERE credit_type IN ({','.join('?' * len(MARGIN_CREDIT_TYPES))})",
            MARGIN_CREDIT_TYPES,
        ).fetchall()
    finally:
        con.close()
    return {str(r[0]).strip() for r in rows}


def scan_symbol(sym: str, g: pd.DataFrame, P: Params, day_idx: dict | None = None) -> list[dict]:
    g = g.reset_index(drop=True)
    o, h, l, c, v = (g[k].to_numpy(float) for k in ("open", "high", "low", "close", "volume"))
    t = g["datetime"].to_numpy()
    n = len(g)
    if n < P.ma + P.down_win + P.confirm_bars + 2:
        return []
    ma = pd.Series(c).rolling(P.ma, min_periods=P.ma).mean().to_numpy()
    ma_down = np.r_[False, np.diff(ma) < 0]
    turnover = pd.Series(c * np.nan_to_num(v)).rolling(60, min_periods=20).mean().to_numpy()
    # この銘柄のデータで営業日が抜けた所 (summary DB が欠けた日) を数える。窓をまたぐイベントは評価から外す
    if day_idx:
        di = g["datetime"].dt.normalize().map(day_idx).to_numpy(int)
        gaps = np.r_[0, np.cumsum(np.diff(di) > 1)]
    else:
        gaps = np.zeros(n, dtype=int)

    out = []
    i = P.ma + P.down_win
    while i < n - P.confirm_bars:
        # クロス: 前の足は 75MA 以下、この足で上
        if not (c[i - 1] <= ma[i - 1] and c[i] > ma[i]):
            i += 1
            continue
        w = slice(i - P.down_win, i)
        below = float(np.mean(c[w] < ma[w]))
        down_frac = float(np.mean(ma_down[w]))
        drop = ma[i - 1] / ma[i - P.down_win] - 1.0
        if below < P.below_frac or down_frac < P.down_slope_frac or drop > P.down_drop:
            i += 1
            continue
        k = i + P.confirm_bars - 1  # 確認足
        above_n = int(np.sum(c[i:k + 1] > ma[i:k + 1]))
        if above_n < P.confirm_min_above or c[k] <= ma[k]:
            i += 1
            continue
        flat_chg = ma[k - P.up_win] / ma[k - P.flat_win] - 1.0
        up_chg = ma[k] / ma[k - P.up_win] - 1.0
        if abs(flat_chg) > P.flat_tol or up_chg <= 0:
            i += 1
            continue

        ev = {
            "symbol": sym, "cross_dt": t[i], "confirm_dt": t[k],
            "below_frac": round(below, 3), "ma_down_frac": round(down_frac, 3), "ma_drop_pct": round(drop * 100, 2),
            "flat_chg_pct": round(flat_chg * 100, 3), "up_chg_pct": round(up_chg * 100, 3),
            "confirm_close": c[k], "confirm_ma": round(ma[k], 2),
            "confirm_ext_pct": round((c[k] / ma[k] - 1) * 100, 2),
        }
        # 押し目待ち: 前の足の 75MA から指値を作る (先読みしない)
        j_end = min(n, k + 1 + P.pullback_max)
        fill = None
        away = P.away_min <= 0 or c[k] >= ma[k] * (1 + P.away_min)
        for j in range(k + 1, j_end):
            if ma[j - 1] < ma[j - 1 - P.up_win]:  # 75MA が下を向き直したら見送り
                ev["status"] = "cancel_ma_turned_down"
                ev["cancel_dt"] = t[j]
                break
            if not away:
                away = h[j - 1] >= ma[j - 1] * (1 + P.away_min)
                if not away:
                    continue
            limit = ma[j - 1] * (1 + P.touch_band)
            if l[j] <= limit:
                fill = (j, min(o[j], limit))
                break
        else:
            ev["status"] = "no_pullback"
        if fill is None:
            out.append(ev)
            i = j_end
            continue

        j, px = fill
        ev.update({"status": "filled", "entry_dt": t[j], "entry_px": round(px, 2),
                   "bars_to_pullback": j - k, "turnover_5m": round(float(turnover[j - 1] or 0))})
        # その後の値動き
        e_end = min(n, j + P.hold_max + 1)
        for hz in (6, 12, 30, 60):
            ev[f"ret_{hz}"] = round((c[j + hz] / px - 1) * 100, 2) if j + hz < n else np.nan
        ev["mfe_pct"] = round((np.max(h[j:e_end]) / px - 1) * 100, 2)
        ev["mae_pct"] = round((np.min(l[j:e_end]) / px - 1) * 100, 2)
        # 簡易ルール: 利確 +take_profit (高値で到達) / 損切り 終値 < 75MA*(1-stop_band) / 時間切れ
        exit_px, exit_reason, x = c[e_end - 1], "time", e_end - 1
        for x in range(j, e_end):
            if h[x] >= px * (1 + P.take_profit) and x > j:
                exit_px, exit_reason = px * (1 + P.take_profit), "take_profit"
                break
            if c[x] < ma[x] * (1 - P.stop_band):
                exit_px, exit_reason = c[x], "stop_ma"
                break
        ev.update({"exit_dt": t[x], "exit_reason": exit_reason, "rule_ret_pct": round((exit_px / px - 1) * 100, 2),
                   "data_gap": bool(gaps[min(n - 1, e_end)] - gaps[max(0, i - P.down_win - P.ma)] > 0)})
        out.append(ev)
        i = e_end
    return out


def cmd_scan(P: Params) -> None:
    bars = pd.read_pickle(CACHE)
    syms = _margin_symbols()
    bars = bars[bars["symbol"].isin(syms)]
    print(f"bars={len(bars)} margin_symbols_with_data={bars['symbol'].nunique()} / flags={len(syms)} "
          f"{bars['datetime'].min():%Y-%m-%d} -> {bars['datetime'].max():%Y-%m-%d}")
    # 営業日 = 100 銘柄以上のデータがある日 (8/29 土曜の 19 銘柄のようなゴミ日は落とす)。
    # ある銘柄にだけその日が無ければ、その銘柄のデータの抜けとして扱う
    norm = bars["datetime"].dt.normalize()
    per_day = bars.groupby(norm)["symbol"].nunique()
    good = per_day[per_day >= 100].index
    bars = bars[norm.isin(good)]
    day_idx = {d: k for k, d in enumerate(sorted(good))}
    t0 = time.time()
    events = []
    for sym, g in bars.groupby("symbol", sort=False):
        events.extend(scan_symbol(sym, g, P, day_idx))
    ev = pd.DataFrame(events)
    print(f"scanned in {time.time() - t0:.1f}s events={len(ev)}")
    if ev.empty:
        return
    os.makedirs(OUT_DIR, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M")
    path = os.path.join(OUT_DIR, f"events_{stamp}.csv")
    ev.to_csv(path, index=False, encoding="utf-8-sig")
    pd.Series(asdict(P)).to_csv(os.path.join(OUT_DIR, f"params_{stamp}.csv"), encoding="utf-8-sig")
    print(ev["status"].value_counts().to_string())
    f = ev[ev["status"] == "filled"]
    if P.min_turnover_5m > 0:
        f = f[f["turnover_5m"] >= P.min_turnover_5m]
    print(f"\nfilled (turnover_5m >= {P.min_turnover_5m:,.0f}): {len(f)}")
    if len(f):
        cols = ["ret_6", "ret_12", "ret_30", "ret_60", "mfe_pct", "mae_pct", "rule_ret_pct"]
        print(f[cols].describe().round(2).to_string())
        print("\nwin rate (rule_ret_pct > 0):", round(float((f["rule_ret_pct"] > 0).mean()) * 100, 1), "%")
        print(f["exit_reason"].value_counts().to_string())
    print(f"\nsaved {path}")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("load")
    a.add_argument("--days", type=int, default=60)
    s = sub.add_parser("scan")
    for k, val in asdict(Params()).items():
        s.add_argument(f"--{k.replace('_', '-')}", type=type(val), default=val)
    args = ap.parse_args()
    if args.cmd == "load":
        cmd_load(args.days)
    else:
        cmd_scan(Params(**{k: getattr(args, k) for k in asdict(Params())}))


if __name__ == "__main__":
    main()
