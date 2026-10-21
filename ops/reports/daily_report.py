# ============================================================
# ops/reports/daily_report.py
# ------------------------------------------------------------
# 2026-09-17 (B7): 1 日の「候補 → ゲート → 発注 → 約定 → 決済」の漏斗と損益を
# ローカルのログ / positions.db から 1 枚にまとめる。
#
#   python ops/reports/daily_report.py [YYYYMMDD] [--out path]
#
# 入力 (すべてローカル。NAS は見ない):
#   %LOCALAPPDATA%\autostock\logs\console\console_YYYYMMDD_*.log  (main.py)
#   %LOCALAPPDATA%\autostock\positions\positions.db
# 出力:
#   標準出力 + %LOCALAPPDATA%\autostock\runtime\reports\daily_YYYYMMDD.md
#
# 毎日の「エントリーが少ない」の切り分けは、まずこの漏斗で
# どの段で落ちているかを見てから個別ログに降りる。
# ============================================================
from __future__ import annotations

import argparse
import collections
import datetime as dt
import glob
import os
import re
import sqlite3
import sys

LOCAL = os.path.join(os.getenv("LOCALAPPDATA") or r"C:\Temp", "autostock")
CONSOLE_DIR = os.path.join(LOCAL, "logs", "console")
POS_DB = os.path.join(LOCAL, "positions", "positions.db")
OUT_DIR = os.path.join(LOCAL, "runtime", "reports")

RE_SKIP = re.compile(r"ENTRY_SKIP (\S+) reason=([A-Z0-9_]+)(?: detail=(\{.*\}))?")
RE_SIDE = re.compile(r"'side': '([A-Z]+)'")
RE_PIPE = re.compile(r"run_entry_pipeline return normalized \{.*?'approved_count': (\d+).*?'skip_reason': '([a-z_]*)'")
RE_SENT = re.compile(r"\[ENTRY (BUY|SELL) SENT\] symbol=(\S+) type=(\S+) oid=(\S+) qty=(\d+)")
RE_CAND = re.compile(r"\[MTF CANDIDATE\] emit symbol=(\S+) side=(\S+) price=(\S+) score=([\d.]+)")
RE_PASSIVE = re.compile(r"\[ENTRY PASSIVE LIMIT\] (retry -> aggressive )?symbol=(\S+) side=(\S+)")
RE_RFB = re.compile(r"\[REST FULL BOARD\] symbol=(\S+) side=(\S+) source=\S+ mode=(\S+) old=(\S+) new=(\S+)")
RE_REQUEUE = re.compile(r"\[SUMMARY RETRY ROTATION\] requeue symbol=(\S+) side=(\S+) round=(\d+)/(\d+)")
RE_MAXROUND = re.compile(r"\[SUMMARY RETRY ROTATION\] max rounds reached symbol=(\S+)")
RE_CANCEL = re.compile(r"\[(ENTRY CANCEL WATCH|PENDING ORDER MONITOR)\][^\n]*?cancel[^\n]*?symbol=(\S+)", re.IGNORECASE)
RE_EXIT = re.compile(r"\[(MTF TREND EXIT|STOP LOSS|BLOWOFF|TRAIL|EXIT SEND|EXIT ORDER)\][^\n]*?symbol=(\S+)")
RE_TS = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
RE_VOL = re.compile(r"\[VOL FILTER\] (ATR|RANGE) (\S+)")
RE_REGNG = re.compile(r"\[REGISTER NG SYMBOLS\][^\n]*symbols=\[([^\]]*)\]")
RE_GATE_DETAIL = re.compile(r"'detail_reason': '([A-Z_]+)")



# 2026-10-01: 損益から差し引くコスト。金利は 9/28 の実測 3 件 (0.00475〜0.00479%/日) から。
#   貸株料は未実測なので金利と同じ値を仮置き。手数料はデイトレ信用で 0 円。
ANNUAL_INTEREST_PCT = float(os.environ.get("REPORT_ANNUAL_INTEREST_PCT", "1.75"))
ANNUAL_LENDING_PCT = float(os.environ.get("REPORT_ANNUAL_LENDING_PCT", "1.75"))
COMMISSION_YEN_PER_TRADE = float(os.environ.get("REPORT_COMMISSION_YEN_PER_TRADE", "0"))

def _fmt_yen(v: float) -> str:
    return f"{v:+,.0f}円"


def scan_logs(day: str) -> dict:
    files = sorted(glob.glob(os.path.join(CONSOLE_DIR, f"console_{day}_*.log")))
    out = {
        "files": files, "lines": 0,
        "skip_by_reason": collections.Counter(),
        "skip_by_reason_side": collections.Counter(),
        "skip_symbols_by_reason": collections.defaultdict(set),
        "gate_detail": collections.Counter(),
        "pipeline_runs": 0, "pipeline_skip": collections.Counter(), "approved_total": 0,
        "sent": [], "candidates": [], "passive_first": 0, "passive_retry": 0,
        "rfb": collections.Counter(), "requeue": [], "maxround": [], "cancels": 0,
        "exits": collections.Counter(), "vol_local": collections.Counter(),
        "register_ng": collections.Counter(), "first_ts": None, "last_ts": None,
        "errors": 0, "hours": collections.Counter(),
    }
    for path in files:
        try:
            fh = open(path, "r", encoding="utf-8", errors="replace")
        except Exception:
            continue
        with fh:
            for line in fh:
                out["lines"] += 1
                m = RE_TS.match(line)
                if m:
                    ts = m.group(1)
                    out["first_ts"] = out["first_ts"] or ts
                    out["last_ts"] = ts
                if "[ERROR]" in line:
                    out["errors"] += 1
                m = RE_SKIP.search(line)
                if m:
                    sym, reason, detail = m.group(1), m.group(2), m.group(3) or ""
                    side = (RE_SIDE.search(detail).group(1) if RE_SIDE.search(detail) else "?")
                    out["skip_by_reason"][reason] += 1
                    out["skip_by_reason_side"][(reason, side)] += 1
                    out["skip_symbols_by_reason"][reason].add(sym)
                    if ts:
                        out["hours"][(ts[11:13], reason)] += 1
                    g = RE_GATE_DETAIL.search(detail)
                    if g and reason in ("MTF_MA_GATE_NG", "MTF_SCORE_GATE_NG"):
                        out["gate_detail"][g.group(1)] += 1
                    continue
                m = RE_PIPE.search(line)
                if m:
                    out["pipeline_runs"] += 1
                    out["approved_total"] += int(m.group(1))
                    out["pipeline_skip"][m.group(2) or "(order)"] += 1
                    continue
                m = RE_SENT.search(line)
                if m:
                    out["sent"].append({"ts": ts, "side": m.group(1), "symbol": m.group(2), "type": m.group(3), "oid": m.group(4), "qty": int(m.group(5))})
                    continue
                m = RE_CAND.search(line)
                if m:
                    out["candidates"].append({"ts": ts, "symbol": m.group(1), "side": m.group(2), "price": m.group(3), "score": float(m.group(4))})
                    continue
                m = RE_PASSIVE.search(line)
                if m:
                    if m.group(1):
                        out["passive_retry"] += 1
                    else:
                        out["passive_first"] += 1
                    continue
                m = RE_RFB.search(line)
                if m:
                    out["rfb"][m.group(3)] += 1
                    continue
                m = RE_REQUEUE.search(line)
                if m:
                    out["requeue"].append({"ts": ts, "symbol": m.group(1), "side": m.group(2), "round": m.group(3)})
                    continue
                m = RE_MAXROUND.search(line)
                if m:
                    out["maxround"].append({"ts": ts, "symbol": m.group(1)})
                    continue
                if RE_CANCEL.search(line):
                    out["cancels"] += 1
                    continue
                m = RE_EXIT.search(line)
                if m:
                    out["exits"][m.group(1)] += 1
                    continue
                m = RE_VOL.search(line)
                if m:
                    out["vol_local"][f"{m.group(1)} {m.group(2)}"] += 1
                    continue
                m = RE_REGNG.search(line)
                if m:
                    for s in m.group(1).replace("'", "").split(","):
                        s = s.strip()
                        if s:
                            out["register_ng"][s] += 1
    return out


def scan_positions(day: str) -> dict:
    out = {"path": POS_DB, "rows": [], "error": ""}
    if not os.path.exists(POS_DB):
        out["error"] = "positions.db missing"
        return out
    d = f"{day[:4]}-{day[4:6]}-{day[6:]}"
    try:
        con = sqlite3.connect(f"file:{POS_DB}?mode=ro", uri=True, timeout=5.0)
        try:
            con.row_factory = sqlite3.Row
            rows = con.execute(
                "SELECT id, symbol, symbolname, side, qty, avg_price, price, exit_price, status, entry_time, exit_time, closed_time "
                "FROM positions WHERE substr(COALESCE(entry_time, created_at, ''),1,10)=? OR substr(COALESCE(exit_time, closed_time, ''),1,10)=? "
                "ORDER BY id", (d, d),
            ).fetchall()
        finally:
            con.close()
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
        return out
    for r in rows:
        r = dict(r)
        try:
            qty = float(r.get("qty") or 0)
            ep = float(r.get("avg_price") or r.get("price") or 0)
            xp = float(r.get("exit_price") or 0)
        except Exception:
            qty = ep = xp = 0.0
        side = str(r.get("side") or "").upper()
        status = str(r.get("status") or "").upper()
        pnl = None
        cost = 0.0
        if xp > 0 and ep > 0 and qty > 0 and status.startswith("CLOSED"):
            is_buy = side.startswith(("BUY", "LONG"))
            pnl = (xp - ep) * qty * (1.0 if is_buy else -1.0)
            # 2026-10-01: 金利 (買い) / 貸株料 (売り) を建玉金額の日割りで引く。
            #   手数料はデイトレ信用で実額 0 円 (9/28・9/30 の約定明細で確認)。
            rate = ANNUAL_INTEREST_PCT if is_buy else ANNUAL_LENDING_PCT
            cost = ep * qty * rate / 365.0 / 100.0 + COMMISSION_YEN_PER_TRADE
        r["pnl"] = pnl
        r["cost"] = cost
        out["rows"].append(r)
    return out


def build_report(day: str, logs: dict, pos: dict) -> str:
    L = []
    d = f"{day[:4]}-{day[4:6]}-{day[6:]}"
    L.append(f"# 日次レポート {d}")
    L.append("")
    L.append(f"- ログ: {len(logs['files'])} ファイル / {logs['lines']:,} 行 ({logs['first_ts']} 〜 {logs['last_ts']}) / [ERROR] {logs['errors']:,} 行")
    L.append("")

    # ---- 損益 ----
    L.append("## 損益 (positions.db ローカル)")
    if pos.get("error"):
        L.append(f"- 読めない: {pos['error']}")
    else:
        closed = [r for r in pos["rows"] if r.get("pnl") is not None]
        open_ = [r for r in pos["rows"] if str(r.get("status") or "").upper() == "OPEN"]
        total = sum(r["pnl"] for r in closed)
        cost_total = sum(r.get("cost") or 0.0 for r in closed)
        wins = [r for r in closed if r["pnl"] - (r.get("cost") or 0.0) > 0]
        L.append(f"- 決済 {len(closed)} 件 / 勝ち {len(wins)} / 実質 {_fmt_yen(total - cost_total)} (総額 {_fmt_yen(total)} / 金利・貸株料・手数料 -{cost_total:,.0f}円) / 未決済 {len(open_)} 件")
        L.append(f"- 注意: 判定価格ベースのシステム記録。実約定は引け後に fill_reconcile.py で取る")
        for r in closed:
            L.append(f"  - {r['symbol']} {r['side']} {int(float(r['qty'] or 0))}株 {r['avg_price']} → {r['exit_price']} = {_fmt_yen(r['pnl'] - (r.get('cost') or 0.0))} (コスト -{(r.get('cost') or 0.0):,.0f}円)  ({str(r.get('entry_time'))[11:19]} → {str(r.get('exit_time') or r.get('closed_time'))[11:19]})")
        for r in open_:
            L.append(f"  - [OPEN] {r['symbol']} {r['side']} {int(float(r['qty'] or 0))}株 @{r['avg_price']} ({str(r.get('entry_time'))[11:19]})")
    L.append("")

    # ---- 発注 ----
    L.append("## 発注")
    L.append(f"- 送信 {len(logs['sent'])} 件 / 未約定キャンセル {logs['cancels']} / 再queue {len(logs['requeue'])} / 打ち切り {len(logs['maxround'])}")
    for s in logs["sent"]:
        L.append(f"  - {s['ts'][11:19]} {s['symbol']} {s['side']} {s['type']} qty={s['qty']} oid={s['oid']}")
    if logs["passive_first"] or logs["passive_retry"]:
        L.append(f"- 受け身指値 (C9): 初回 {logs['passive_first']} / 再queue後の攻め {logs['passive_retry']}")
    if logs["rfb"]:
        L.append("- REST 板の値決め: " + ", ".join(f"{k}={v}" for k, v in logs["rfb"].most_common()))
    if logs["exits"]:
        L.append("- 決済トリガ: " + ", ".join(f"{k}={v}" for k, v in logs["exits"].most_common()))
    L.append("")

    # ---- 漏斗 ----
    L.append("## エントリー漏斗")
    L.append(f"- pipeline 実行 {logs['pipeline_runs']} 回 / approved 合計 {logs['approved_total']} / 結果: " + ", ".join(f"{k}={v}" for k, v in logs["pipeline_skip"].most_common()))
    if logs["candidates"]:
        L.append(f"- 3m/5m 候補ソース (B6): {len(logs['candidates'])} 件 " + ", ".join(f"{c['symbol']}{'B' if c['side']=='BUY' else 'S'}" for c in logs["candidates"][:30]))
    L.append("- ENTRY_SKIP 理由 (件数 / 銘柄数 / BUY:SELL):")
    for reason, n in logs["skip_by_reason"].most_common(25):
        b = logs["skip_by_reason_side"][(reason, "BUY")]
        s = logs["skip_by_reason_side"][(reason, "SELL")]
        L.append(f"  - {reason}: {n} / {len(logs['skip_symbols_by_reason'][reason])} / {b}:{s}")
    if logs["gate_detail"]:
        L.append("- MTF ゲート内訳: " + ", ".join(f"{k}={v}" for k, v in logs["gate_detail"].most_common(8)))
    if logs["vol_local"]:
        L.append("- VOL FILTER ローカル足の使用: " + ", ".join(f"{k}={v}" for k, v in logs["vol_local"].most_common(6)))
    if logs["register_ng"]:
        L.append(f"- PUSH 登録 NG 銘柄 (A4): {len(logs['register_ng'])} 銘柄 " + ", ".join(f"{k}x{v}" for k, v in logs["register_ng"].most_common(10)))
    L.append("")

    # ---- 時間帯別 ----
    top_reasons = [r for r, _ in logs["skip_by_reason"].most_common(5)]
    if top_reasons:
        L.append("## 時間帯別 ENTRY_SKIP (上位理由)")
        L.append("| 時 | " + " | ".join(top_reasons) + " |")
        L.append("|---|" + "---|" * len(top_reasons))
        for h in [f"{i:02d}" for i in range(9, 16)]:
            vals = [logs["hours"][(h, r)] for r in top_reasons]
            if any(vals):
                L.append(f"| {h} | " + " | ".join(str(v) for v in vals) + " |")
        L.append("")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("day", nargs="?", default=dt.date.today().strftime("%Y%m%d"))
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    day = args.day.replace("-", "")
    logs = scan_logs(day)
    pos = scan_positions(day)
    text = build_report(day, logs, pos)
    out = args.out or os.path.join(OUT_DIR, f"daily_{day}.md")
    try:
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    except Exception as e:
        text += f"\n(write failed: {e})"
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(text)
    print(f"\n-> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
