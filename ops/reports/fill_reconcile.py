# -*- coding: utf-8 -*-
# ============================================================
# ops/reports/fill_reconcile.py   (2026-09-28)
# ------------------------------------------------------------
# 証券会社の実約定でその日の損益を作り直す。
#
# なぜ必要か (2026-09-28 に判明):
#   システムの EXIT COMPLETE が記録する pnl は「判定時の価格」から計算した
#   想定値で、(1) 実際の約定価格 (2) 金利 を含まない。さらに決済発注 68 件に
#   対し EXIT COMPLETE は 41 件しかなく、40% は損益が記録されていない。
#
#   9/28 前場の実測:
#     システム集計   +220 円
#     取引明細       -3,058 円
#
#   差の内訳:
#     ・金利     建玉金額の 0.0048%/日 (年率 1.75%) で 3 件が正確に一致
#                  9984 628,000円 -> -30円 / 7220 313,400円 -> -15円 /
#                  621A 147,490円 -> -7円
#     ・約定ずれ 約定確認ログが無い決済は判定価格のまま記録される。
#                  621A は 1478 で発注して約定 1466 (-12円)、
#                  1470 で発注して約定 1464 (-6円)。
#                  1 分で 1481 -> 1470 と急落する場面の成行売り。
#
#   この状態でパラメータを調整すると「システムは +220 円、実際は -3,058 円」
#   のまま判断を誤り続ける。損益の正を証券会社側に置く。
#
# 使い方 (引け後、システムを止めてから):
#   powershell -File C:\autostock\src\ops\local_run\stop_all.ps1
#   C:\autostock\venv\Scripts\python.exe C:\autostock\src\ops\reports\fill_reconcile.py
#
#   --keep-running  稼働中でも実行する (トークンを奪うので通常は使わない)
#   --date          既に保存した JSONL を読み直して再集計する (API を叩かない)
#
# なぜ稼働中に走らせないか:
#   kabu Station の API トークンは 1 セッション 1 つで、後から取得した側が
#   先のトークンを 401 にする。稼働中に叩くと本番の発注と決済が止まる。
#   起動中のプロセスを検出したら既定で中止する。
#
# 出力:
#   %LOCALAPPDATA%\autostock\runtime\reports\fills_<YYYYMMDD>.jsonl
#     /orders の生データ (約定明細つき) をそのまま残す。後から再集計できる。
#   %LOCALAPPDATA%\autostock\runtime\reports\fill_reconcile_<YYYYMMDD>.md
#     銘柄ごと・決済ごとの損益と、システム記録との差。
# ============================================================
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# 信用買いの金利 (年率)。建玉金額 x この率 / 365 が 1 日分。
# 9/28 の実測 3 件が -0.00478% / -0.00479% / -0.00475% で一致した。
DEFAULT_ANNUAL_INTEREST_PCT = 1.75

CASH_MARGIN_NEW = 2      # 信用新規
CASH_MARGIN_CLOSE = 3    # 信用返済
SIDE_SELL = "1"
SIDE_BUY = "2"


def _local_base() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("TEMP") or "."
    return Path(base) / "autostock"


def _reports_dir() -> Path:
    d = _local_base() / "runtime" / "reports"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _positions_db() -> Path:
    return _local_base() / "positions" / "positions.db"


def _running_autostock_pids() -> list[int]:
    """稼働中の autostock プロセスを返す (トークン衝突を避けるため)。"""
    out: list[int] = []
    try:
        import subprocess

        ps = (
            "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
            "Where-Object { $_.CommandLine -like '*autostock*src*' } | "
            "ForEach-Object { $_.ProcessId }"
        )
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, timeout=30,
        )
        for line in (r.stdout or "").splitlines():
            line = line.strip()
            if line.isdigit():
                out.append(int(line))
    except Exception:
        pass
    return out


def fetch_orders() -> list[dict]:
    """/orders を 1 回だけ叩いて当日注文を全部取る。"""
    from kabu_api.orders import get_all_orders

    data = get_all_orders() or []
    if not isinstance(data, list):
        return []
    return data


def save_raw(orders: list[dict], ymd: str) -> Path:
    p = _reports_dir() / ("fills_%s.jsonl" % ymd)
    with open(p, "w", encoding="utf-8") as f:
        for o in orders:
            f.write(json.dumps(o, ensure_ascii=False) + "\n")
    return p


def load_raw(ymd: str) -> list[dict]:
    p = _reports_dir() / ("fills_%s.jsonl" % ymd)
    if not p.exists():
        return []
    out = []
    with open(p, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except Exception:
                    pass
    return out


def _f(v, d=0.0) -> float:
    try:
        return float(v)
    except Exception:
        return d


def executions(order: dict) -> list[tuple[float, float]]:
    """その注文の約定 (数量, 単価) を返す。約定していなければ空。

    2026-09-28: 実際の /orders では約定明細は RecType=8 で、数量と単価は
    ExecutionQty / ExecutionPrice ではなく **Qty / Price** に入っている
    (ExecutionID が付くのが約定行の印)。kabu_api/info.py が古い形式の
    ままだったのでそれを参考にして全部 None を拾い、合計 0 円になっていた。
    """
    out = []
    for d in order.get("Details") or []:
        try:
            rec = int(_f(d.get("RecType"), 0))
        except Exception:
            rec = 0
        if rec != 8 and not d.get("ExecutionID"):
            continue
        q = _f(d.get("Qty"), 0.0) or _f(d.get("ExecutionQty"), 0.0)
        px = _f(d.get("Price"), 0.0) or _f(d.get("ExecutionPrice"), 0.0)
        if q > 0 and px > 0:
            out.append((q, px))
    return out


def fees(order: dict) -> float:
    """手数料と消費税の実額 (Details の Commission / CommissionTax)。"""
    t = 0.0
    for d in order.get("Details") or []:
        t += _f(d.get("Commission"), 0.0) + _f(d.get("CommissionTax"), 0.0)
    return t


def order_day(order: dict) -> str:
    """注文 ID の先頭 8 桁 (YYYYMMDD)。読めなければ空。

    2026-09-28: /orders は当日注文だけのはずだが、過去日の注文 (同日に決済まで
    終わったもの) も返ってくる。9/28 の 621A には 9/24 の建玉
    (ID=20260924A02N44633380) が混ざっており、銘柄でまとめると 800 株・
    新規平均 1478.59 になって 9/24 分を 9/28 の損益として数えてしまった。
    """
    s = str(order.get("ID") or order.get("OrderId") or "").strip()
    if len(s) >= 8 and s[:8].isdigit():
        return s[:8]
    return ""


def avg_fill(order: dict) -> tuple[float, float]:
    """(約定数量, 平均約定単価)。"""
    ex = executions(order)
    q = sum(x[0] for x in ex)
    if q <= 0:
        return 0.0, 0.0
    amt = sum(x[0] * x[1] for x in ex)
    return q, amt / q


def entry_price_map(ymd: str) -> dict[str, list[tuple[float, float]]]:
    """positions.db から銘柄ごとの (数量, 建値) を取る。"""
    out: dict[str, list[tuple[float, float]]] = {}
    p = _positions_db()
    if not p.exists():
        return out
    d = "%s-%s-%s" % (ymd[:4], ymd[4:6], ymd[6:])
    try:
        c = sqlite3.connect("file:%s?mode=ro" % p, uri=True)
        for sym, qty, ap in c.execute(
            "select symbol, qty, avg_price from positions "
            "where date(entry_time)=? and qty>0 and avg_price>0",
            (d,),
        ):
            out.setdefault(str(sym), []).append((_f(qty), _f(ap)))
        c.close()
    except Exception:
        pass
    return out


def system_pnl_from_logs(ymd: str) -> dict[str, float]:
    """比較用に EXIT COMPLETE の合計をログから拾う。"""
    import glob
    import re

    pat = re.compile(r"EXIT COMPLETE.*?symbol=([0-9A-Z]+)\s+pnl=(-?[0-9.]+)")
    out: dict[str, float] = {}
    base = _local_base() / "logs" / "console"
    for f in sorted(glob.glob(str(base / ("console_%s_*.log" % ymd)))):
        try:
            with open(f, "rb") as fh:
                for line in fh:
                    s = line.decode("utf-8", "ignore")
                    if "EXIT COMPLETE" not in s:
                        continue
                    m = pat.search(s)
                    if m:
                        out[m.group(1)] = out.get(m.group(1), 0.0) + float(m.group(2))
        except Exception:
            pass
    return out


def build_report(orders: list[dict], ymd: str, annual_pct: float, only_day: bool = True) -> str:
    entries = entry_price_map(ymd)
    sys_pnl = system_pnl_from_logs(ymd)

    # 新規 (CashMargin=2) と返済 (CashMargin=3) に分ける
    news: dict[str, list[tuple[float, float]]] = {}
    closes: dict[str, list[tuple[str, float, float]]] = {}
    skipped_other_day = 0
    for o in orders:
        sym = str(o.get("Symbol") or "").strip()
        if not sym:
            continue
        # 2026-09-28: 過去日の注文が混ざるので対象日だけに絞る。
        od = order_day(o)
        if only_day and od and od != ymd:
            skipped_other_day += 1
            continue
        cm = int(_f(o.get("CashMargin"), 0))
        q, px = avg_fill(o)
        if q <= 0:
            continue
        if cm == CASH_MARGIN_NEW:
            news.setdefault(sym, []).append((q, px))
        elif cm == CASH_MARGIN_CLOSE:
            closes.setdefault(sym, []).append((str(o.get("ID") or o.get("OrderId") or ""), q, px))

    lines = []
    lines.append("# 実約定による損益照合 %s-%s-%s" % (ymd[:4], ymd[4:6], ymd[6:]))
    lines.append("")
    lines.append("損益の正は証券会社の約定。システムの EXIT COMPLETE は判定価格から")
    lines.append("計算した想定値で、実約定と金利を含まないため参考値として並記する。")
    lines.append("")
    lines.append("金利は年率 %.2f%% (建玉金額 x %.5f%%/日) で計算。" % (
        annual_pct, annual_pct / 365.0))
    if only_day:
        lines.append("")
        lines.append("注文 ID の先頭 8 桁で対象日を絞っている "
                     "(除外 %d 件)。/orders には過去日の注文も混ざるため。" % skipped_other_day)
    lines.append("")

    total_gross = 0.0
    total_interest = 0.0
    lines.append("| 銘柄 | 新規平均 | 返済平均 | 数量 | 総額損益 | 金利 | 実質損益 |")
    lines.append("|---|---|---|---|---|---|---|")
    for sym in sorted(set(list(news) + list(closes))):
        nq = sum(q for q, _ in news.get(sym, []))
        namt = sum(q * p for q, p in news.get(sym, []))
        navg = namt / nq if nq else 0.0
        cq = sum(q for _, q, _ in closes.get(sym, []))
        camt = sum(q * p for _, q, p in closes.get(sym, []))
        cavg = camt / cq if cq else 0.0
        if nq <= 0 or cq <= 0:
            lines.append("| %s | %s | %s | 新規/返済が揃っていない | | | |" % (
                sym, "%.2f" % navg if nq else "-", "%.2f" % cavg if cq else "-"))
            continue
        q = min(nq, cq)
        gross = (cavg - navg) * q
        interest = navg * q * (annual_pct / 365.0) / 100.0
        net = gross - interest
        total_gross += gross
        total_interest += interest
        lines.append("| %s | %.2f | %.2f | %.0f | %+.0f | -%.0f | **%+.0f** |" % (
            sym, navg, cavg, q, gross, interest, net))
    lines.append("")
    lines.append("**実質損益 合計 %+.0f 円** (総額 %+.0f / 金利 -%.0f)" % (
        total_gross - total_interest, total_gross, total_interest))
    lines.append("")

    if sys_pnl:
        lines.append("## システム記録との差")
        lines.append("")
        lines.append("| 銘柄 | システム記録 | 実質損益 | 差 |")
        lines.append("|---|---|---|---|")
        for sym in sorted(set(list(sys_pnl) + list(closes))):
            nq = sum(q for q, _ in news.get(sym, []))
            namt = sum(q * p for q, p in news.get(sym, []))
            navg = namt / nq if nq else 0.0
            cq = sum(q for _, q, _ in closes.get(sym, []))
            camt = sum(q * p for _, q, p in closes.get(sym, []))
            cavg = camt / cq if cq else 0.0
            if nq <= 0 or cq <= 0:
                continue
            q = min(nq, cq)
            net = (cavg - navg) * q - navg * q * (annual_pct / 365.0) / 100.0
            s = sys_pnl.get(sym)
            lines.append("| %s | %s | %+.0f | %s |" % (
                sym, ("%+.0f" % s) if s is not None else "記録なし", net,
                ("%+.0f" % (net - s)) if s is not None else "-"))
        lines.append("")
        lines.append("システム合計 %+.0f 円 / 実質損益 合計 %+.0f 円" % (
            sum(sys_pnl.values()), total_gross - total_interest))
        lines.append("")

    lines.append("## 返済注文ごとの約定")
    lines.append("")
    lines.append("| 銘柄 | 注文ID | 数量 | 平均約定 |")
    lines.append("|---|---|---|---|")
    for sym in sorted(closes):
        for oid, q, px in closes[sym]:
            lines.append("| %s | %s | %.0f | %.2f |" % (sym, oid, q, px))
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=dt.date.today().strftime("%Y%m%d"),
                    help="対象日 (YYYYMMDD)。既に保存した JSONL を使う")
    ap.add_argument("--offline", action="store_true",
                    help="API を叩かず保存済み JSONL だけで再集計する")
    ap.add_argument("--keep-running", action="store_true",
                    help="稼働中でも実行する (トークンを奪うので通常は使わない)")
    ap.add_argument("--annual-interest-pct", type=float,
                    default=DEFAULT_ANNUAL_INTEREST_PCT)
    ap.add_argument("--all-days", action="store_true",
                    help="注文 ID の日付で絞らず全部集計する (過去分もまとめて見るとき)")
    a = ap.parse_args()

    ymd = a.date
    if a.offline:
        orders = load_raw(ymd)
        if not orders:
            print("NG: 保存済みの fills_%s.jsonl が無い" % ymd)
            return 1
        print("保存済み %d 件を読み直した" % len(orders))
    else:
        pids = _running_autostock_pids()
        if pids and not a.keep_running:
            print("中止: autostock が稼働中 (pid=%s)。" % ",".join(map(str, pids)))
            print("  kabu Station の API トークンは 1 セッション 1 つで、後から取得した")
            print("  側が先のトークンを 401 にする。稼働中に叩くと本番の発注と決済が")
            print("  止まる。stop_all.ps1 で止めてから実行すること。")
            print("  どうしても必要なら --keep-running を付ける。")
            return 2
        orders = fetch_orders()
        if not orders:
            print("NG: /orders が空。kabu Station にログインしているか確認する")
            return 1
        p = save_raw(orders, ymd)
        print("生データを保存: %s (%d 件)" % (p, len(orders)))

    md = build_report(orders, ymd, a.annual_interest_pct, only_day=not a.all_days)
    out = _reports_dir() / ("fill_reconcile_%s.md" % ymd)
    out.write_text(md, encoding="utf-8")
    print()
    print(md)
    print()
    print("-> %s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
