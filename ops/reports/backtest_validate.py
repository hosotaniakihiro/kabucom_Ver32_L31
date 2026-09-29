# -*- coding: utf-8 -*-
# ============================================================
# ops/reports/backtest_validate.py   (2026-09-28)
# ------------------------------------------------------------
# 出口モデルが「証券会社の実約定」を再現できるか検算する。
#
# なぜ必要か:
#   9/19 に 9/18 の 16 件だけで「トレール主体が最良」と結論し、9/24・9/25 を
#   全敗にした。さらに 9/28 に、そのとき使っていた損益自体が実態の 1/4 しか
#   見ていなかったことが判明した (判定価格ベース / 40% 未記録 / 金利なし)。
#   モデルで最適化する前に、**実績を再現できることを確認する**。
#
# やること:
#   実際のエントリー (positions.db の建値・株数・時刻) を起点に、
#   backtest.db の 1 分足で出口ルールを再生し、
#   /orders の実約定損益 (fills_<YYYYMMDD>.jsonl) と突き合わせる。
#
#   エントリー側は再現しない。ここで検証したいのは「出口の挙動とスリッページ」
#   だから。エントリー条件の再現は出口が合ってから。
#
# 出口ルール (実装に合わせた順序):
#   1) 建値ストップ  entry * (1 - stop/100)
#   2) FULL_TAKE     entry * (1 + take/100)
#   3) トレール      高値 * (1 - trail/100)   ※含み益が出てから
#   4) 15:20 で成行
#   同一分に複数成立するときは不利側 (ストップ) を先に取る。
#
# スリッページ:
#   決済は成行なので実約定は判定価格より不利になる。9/28 の実測では
#     621A  判定 1478 -> 約定 1466 (-12 円 / -0.81%)
#     621A  判定 1470 -> 約定 1464 ( -6 円 / -0.41%)
#     7220  判定 3130 -> 約定 3130 (  0 円)  ※約定確認が取れたもの
#     9984  判定 6286 -> 約定 6286 (  0 円)
#   --slip-pct で一律の率を与え、実績に合う値を探す。
#
# 使い方:
#   C:\autostock\venv\Scripts\python.exe ops\reports\backtest_validate.py --date 20260928
#   ... --date 20260928 --scan-slip     スリッページ率を振って実績に近い値を探す
# ============================================================
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

ANNUAL_INTEREST_PCT = 1.75


def _local() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("TEMP") or "."
    return Path(base) / "autostock"


def bt_db() -> Path:
    return _local() / "backtest" / "backtest.db"


def positions_db() -> Path:
    return _local() / "positions" / "positions.db"


def fills_path(ymd: str) -> Path:
    return _local() / "runtime" / "reports" / ("fills_%s.jsonl" % ymd)


def _f(v, d=0.0) -> float:
    try:
        return float(v)
    except Exception:
        return d


# ------------------------------------------------------------
# 実績 (証券会社)
# ------------------------------------------------------------
def actual_pnl(ymd: str) -> dict[str, dict]:
    """銘柄ごとの実約定 (新規平均 / 返済平均 / 数量 / 実質損益)。"""
    p = fills_path(ymd)
    if not p.exists():
        return {}
    orders = []
    with open(p, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                orders.append(json.loads(line))

    def execs(o):
        out = []
        for d in o.get("Details") or []:
            if int(_f(d.get("RecType"), 0)) != 8 and not d.get("ExecutionID"):
                continue
            q, px = _f(d.get("Qty")), _f(d.get("Price"))
            if q > 0 and px > 0:
                out.append((q, px))
        return out

    news, closes = {}, {}
    for o in orders:
        sym = str(o.get("Symbol") or "").strip()
        cm = int(_f(o.get("CashMargin"), 0))
        ex = execs(o)
        if not sym or not ex:
            continue
        q = sum(x[0] for x in ex)
        amt = sum(x[0] * x[1] for x in ex)
        d = news if cm == 2 else (closes if cm == 3 else None)
        if d is None:
            continue
        a = d.setdefault(sym, [0.0, 0.0])
        a[0] += q
        a[1] += amt

    out = {}
    for sym in set(list(news) + list(closes)):
        nq, namt = news.get(sym, [0.0, 0.0])
        cq, camt = closes.get(sym, [0.0, 0.0])
        if nq <= 0 or cq <= 0:
            continue
        navg, cavg = namt / nq, camt / cq
        q = min(nq, cq)
        gross = (cavg - navg) * q
        interest = navg * q * (ANNUAL_INTEREST_PCT / 365.0) / 100.0
        out[sym] = dict(entry=navg, exit=cavg, qty=q, gross=gross,
                        interest=interest, net=gross - interest)
    return out


# ------------------------------------------------------------
# エントリー (システム)
# ------------------------------------------------------------
def entries(ymd: str) -> list[tuple[str, str, float, int]]:
    """(symbol, entry_time, avg_price, qty) を分単位でまとめて返す。"""
    d = "%s-%s-%s" % (ymd[:4], ymd[4:6], ymd[6:])
    c = sqlite3.connect("file:%s?mode=ro" % positions_db(), uri=True)
    rows = list(c.execute(
        "select symbol, entry_time, avg_price, qty from positions "
        "where date(entry_time)=? and qty>0 and avg_price>0 order by entry_time", (d,)))
    c.close()
    agg: dict[tuple[str, str], list] = {}
    for sym, et, ap, q in rows:
        k = (str(sym), et[:16])
        a = agg.setdefault(k, [0, 0.0, 0.0])
        a[0] += int(q)
        a[1] += float(ap) * int(q)
        a[2] += int(q)
    out = []
    for (sym, t), (q, amt, qq) in sorted(agg.items(), key=lambda kv: kv[0][1]):
        out.append((sym, t, amt / qq, q))
    return out


# ------------------------------------------------------------
# 1 分足
# ------------------------------------------------------------
_bar_cache: dict[tuple[str, str], list] = {}


def bars(ymd: str, sym: str) -> list[tuple[str, float, float, float]]:
    k = (ymd, sym)
    if k in _bar_cache:
        return _bar_cache[k]
    c = sqlite3.connect("file:%s?mode=ro" % bt_db(), uri=True)
    rows = list(c.execute(
        "select datetime, close, high, low from bar1m where day=? and symbol=? "
        "and close>0 order by datetime", (ymd, sym)))
    c.close()
    out = [(r[0], float(r[1]), float(r[2] or r[1]), float(r[3] or r[1])) for r in rows]
    _bar_cache[k] = out
    return out


def simulate(ymd: str, sym: str, t_entry: str, entry: float, qty: int,
             stop: float, take: float, trail: float, slip_pct: float):
    """出口を再生して (実質損益, 決済価格, 理由) を返す。"""
    b = [x for x in bars(ymd, sym) if x[0][11:16] >= t_entry[11:16]]
    if not b:
        return None
    stop_px = entry * (1 - stop / 100.0)
    take_px = entry * (1 + take / 100.0) if take > 0 else None
    hi = entry
    exit_px, reason = None, None
    for t, close, high, low in b:
        if t[11:16] >= "15:20":
            exit_px, reason = close, "EOD"
            break
        if low <= stop_px:                      # 安値でストップに触れる
            exit_px, reason = stop_px, "STOP"
            break
        if take_px is not None and high >= take_px:
            exit_px, reason = take_px, "TAKE"
            break
        hi = max(hi, high)
        if trail > 0:
            tr = hi * (1 - trail / 100.0)
            if hi > entry and low <= tr:
                exit_px, reason = tr, "TRAIL"
                break
    if exit_px is None:
        exit_px, reason = b[-1][1], "EOD"
    # 成行の不利側スリッページ
    fill = exit_px * (1 - slip_pct / 100.0)
    gross = (fill - entry) * qty
    interest = entry * qty * (ANNUAL_INTEREST_PCT / 365.0) / 100.0
    return gross - interest, fill, reason


def run(ymd: str, stop: float, take: float, trail: float, slip_pct: float,
        verbose: bool = True):
    act = actual_pnl(ymd)
    ent = entries(ymd)
    rows = []
    for sym, t, px, q in ent:
        r = simulate(ymd, sym, t, px, q, stop, take, trail, slip_pct)
        if r is None:
            rows.append((sym, t, px, q, None, None, None))
            continue
        net, fill, reason = r
        rows.append((sym, t, px, q, net, fill, reason))

    # 銘柄単位で合算 (実績は銘柄単位でしか出せない)
    sim_by_sym: dict[str, float] = {}
    for sym, t, px, q, net, fill, reason in rows:
        if net is None:
            continue
        sim_by_sym[sym] = sim_by_sym.get(sym, 0.0) + net

    if verbose:
        print("設定: ストップ %.2f%% / FULL_TAKE %s / トレール %.2f%% / スリッページ %.3f%%" % (
            stop, ("%.2f%%" % take) if take > 0 else "無効", trail, slip_pct))
        print()
        print("銘柄   時刻   株数    建値     再生の決済  理由    再生損益    実績損益      差")
        for sym, t, px, q, net, fill, reason in rows:
            a = act.get(sym)
            if net is None:
                print("%-5s %s %5d %8.2f      データ無し" % (sym, t[11:16], q, px))
                continue
            print("%-5s %s %5d %8.2f %11.2f  %-6s %+9.0f  %s  %s" % (
                sym, t[11:16], q, px, fill, reason, net,
                ("%+9.0f" % a["net"]) if a else "   実績なし",
                ("%+8.0f" % (net - a["net"])) if a else "        -"))
        print()

    # 実績と再生の両方がある銘柄だけで合計を比べる
    common = [s for s in sim_by_sym if s in act]
    sim_tot = sum(sim_by_sym[s] for s in common)
    act_tot = sum(act[s]["net"] for s in common)
    return sim_tot, act_tot, len(common), sim_by_sym, act


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="20260928")
    ap.add_argument("--stop", type=float, default=0.30)
    ap.add_argument("--take", type=float, default=0.80)
    ap.add_argument("--trail", type=float, default=0.20)
    ap.add_argument("--slip-pct", type=float, default=0.0)
    ap.add_argument("--scan-slip", action="store_true")
    a = ap.parse_args()

    if not bt_db().exists():
        print("NG: backtest.db が無い。先に backtest_extract.py を実行する")
        return 1
    if not fills_path(a.date).exists():
        print("NG: fills_%s.jsonl が無い。先に fill_reconcile.py を実行する" % a.date)
        return 1

    if a.scan_slip:
        print("=== スリッページ率を振って実績に近い値を探す ===")
        print("  slip     再生合計     実績合計        差     比")
        best = None
        for sp in (0.0, 0.05, 0.10, 0.15, 0.20, 0.30, 0.40, 0.60):
            s, t, n, _, _ = run(a.date, a.stop, a.take, a.trail, sp, verbose=False)
            diff = s - t
            print("  %.2f%%  %+10.0f  %+10.0f  %+9.0f  %5.2f" % (
                sp, s, t, diff, (s / t) if t else 0))
            if best is None or abs(diff) < abs(best[1]):
                best = (sp, diff)
        print()
        print("実績に最も近いのは slip=%.2f%% (差 %+.0f 円)" % best)
        print()
        print("=== その設定で銘柄ごとの内訳 ===")
        run(a.date, a.stop, a.take, a.trail, best[0], verbose=True)
        return 0

    s, t, n, _, _ = run(a.date, a.stop, a.take, a.trail, a.slip_pct, verbose=True)
    print("突き合わせ %d 銘柄:  再生 %+.0f 円 / 実績 %+.0f 円 / 差 %+.0f 円" % (n, s, t, s - t))
    return 0


if __name__ == "__main__":
    sys.exit(main())
