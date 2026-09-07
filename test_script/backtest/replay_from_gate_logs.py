# -*- coding: utf-8 -*-
# ============================================================
# File   : test_script/backtest/replay_from_gate_logs.py
# Version: REPLAY-FROM-GATE-LOGS-V1
# ------------------------------------------------------------
# 「エントリー候補と判断した地点で実際に入っていたらどうなったか」
# を、保存済みの tick から再現する。
# ============================================================
r"""ゲートのログに残った候補地点でエントリーした場合の損益を再現する。

既存の trading/backtest_replay/ は trade_audit / executions_runtime
(= 実発注の記録) を読む作りなので、**発注が 1 件も無い日には使えない**。
2026-09-07 時点で発注は 0 件なので、こちらは別の入力を使う:

  候補地点   : コンソールログの AI_GATE_OK / ENTRY_SKIP / MA5 GUARD
  その後の値動き : push{YYYYMMDD}.db の stream_data (tick)
  手仕舞い条件  : settings.ini の [exit]

実注文は一切出さない。DB は read-only で開く。稼働中でも安全。

使い方:
    python test_script/backtest/replay_from_gate_logs.py
    python test_script/backtest/replay_from_gate_logs.py --date 20260907
    python test_script/backtest/replay_from_gate_logs.py --sweep
    python test_script/backtest/replay_from_gate_logs.py --json out.json
"""
from __future__ import annotations

import argparse
import configparser
import datetime as dt
import glob
import json
import os
import re
import sqlite3
import sys
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ============================================================
# 候補地点の抽出
# ============================================================

# ✅ AI_GATE_OK symbol=6613 side=BUY detail=...
RE_GATE_OK = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})[,.]?\d*\s.*?AI_GATE_OK\s+symbol=(?P<symbol>[0-9A-Z]+)\s+side=(?P<side>BUY|SELL)"
)
# ⛔ ENTRY_SKIP 4588 reason=ATR_1M_FILTER_NG detail={'side': 'BUY'}
RE_SKIP = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})[,.]?\d*\s.*?ENTRY_SKIP\s+(?P<symbol>[0-9A-Z]+)\s+reason=(?P<reason>[A-Z_0-9]+).*?'side':\s*'(?P<side>BUY|SELL)'"
)
# [ENTRY MA5 THIRD BAR GUARD] filtered symbol=6613 before=1 after=0 skipped=[{'symbol': '6613', 'side': 'BUY',
RE_MA5 = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})[,.]?\d*\s.*?ENTRY MA5 THIRD BAR GUARD\].*?symbol=(?P<symbol>[0-9A-Z]+)\s+before=\d+\s+after=0.*?'side':\s*'(?P<side>BUY|SELL)'"
)


@dataclass
class Candidate:
    ts: dt.datetime
    symbol: str
    side: str
    blocked_by: Optional[str]   # None = 発注まで到達していた

    def key(self) -> Tuple[str, str, str]:
        return (self.symbol, self.side, self.ts.strftime("%Y-%m-%d %H:%M:%S"))


def log_dirs() -> List[Path]:
    dirs: List[Path] = []
    try:
        from data_collectors.config import LOG_DIR

        dirs.append(Path(LOG_DIR))
    except Exception:
        pass
    dirs.append(Path(r"X:\logs\console"))
    return [d for d in dirs if d.exists()]


def collect_candidates(date_str: str) -> List[Candidate]:
    """その日のログから候補地点を集める。

    同じ (symbol, side) が 1 分以内に何度も出るので、分単位で 1 件に畳む
    (実運用でも同じ足で二重に建てることはない)。
    """
    seen: Dict[Tuple[str, str, str], Candidate] = {}

    for d in log_dirs():
        for path in sorted(glob.glob(str(d / f"*{date_str}*.log"))):
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        if "AI_GATE_OK" not in line and "ENTRY_SKIP" not in line and "MA5 THIRD BAR GUARD" not in line:
                            continue
                        m = RE_GATE_OK.match(line)
                        blocked = None
                        if not m:
                            m = RE_SKIP.match(line)
                            if m:
                                blocked = m.group("reason")
                        if not m:
                            m = RE_MA5.match(line)
                            if m:
                                blocked = "MA5_THIRD_BAR_GUARD"
                        if not m:
                            continue

                        ts = dt.datetime.strptime(m.group("ts"), "%Y-%m-%d %H:%M:%S")
                        minute = ts.replace(second=0)
                        c = Candidate(ts=ts, symbol=m.group("symbol"), side=m.group("side"), blocked_by=blocked)
                        k = (c.symbol, c.side, minute.strftime("%Y-%m-%d %H:%M"))
                        prev = seen.get(k)
                        # ブロック理由つきを優先して残す (どのフィルタで消えたかを知りたい)
                        if prev is None or (prev.blocked_by is None and blocked is not None):
                            seen[k] = c
            except Exception as e:  # noqa: BLE001
                print(f"  ! ログ読み込み失敗 {path}: {e}")

    out = sorted(seen.values(), key=lambda c: (c.ts, c.symbol))
    return out


# ============================================================
# tick の読み込み
# ============================================================

def push_db_path_for(date_str: str) -> Optional[Path]:
    cands: List[Path] = []
    try:
        from trading.push import push_db_location

        cands.append(push_db_location.push_db_dir() / f"push{date_str}.db")
        cands.append(push_db_location.nas_push_db_dir() / f"push{date_str}.db")
    except Exception:
        pass
    cands.append(Path(r"X:\raw_data\kabu_station\push") / f"push{date_str}.db")
    for c in cands:
        try:
            if c.exists():
                return c
        except Exception:
            continue
    return None


def load_ticks(db: Path, symbols: Iterable[str]) -> Dict[str, List[Tuple[dt.datetime, float]]]:
    """銘柄ごとの (時刻, 価格) 列。datetime 昇順。"""
    syms = sorted({str(s) for s in symbols})
    if not syms:
        return {}
    out: Dict[str, List[Tuple[dt.datetime, float]]] = {s: [] for s in syms}
    conn = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=30.0)
    try:
        conn.execute("PRAGMA busy_timeout=15000")
        qmarks = ",".join("?" * len(syms))
        cur = conn.execute(
            f"SELECT symbol, datetime, price FROM stream_data "
            f"WHERE symbol IN ({qmarks}) AND price IS NOT NULL ORDER BY symbol, datetime",
            syms,
        )
        for sym, ts_s, price in cur:
            t = _parse_dt(ts_s)
            if t is None:
                continue
            try:
                p = float(price)
            except Exception:
                continue
            if p <= 0:
                continue
            out.setdefault(str(sym), []).append((t, p))
    finally:
        conn.close()
    return out


def _parse_dt(s: Any) -> Optional[dt.datetime]:
    if not s:
        return None
    txt = str(s).strip()
    try:
        t = dt.datetime.fromisoformat(txt)
        return t.replace(tzinfo=None)
    except Exception:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return dt.datetime.strptime(txt[:26], fmt)
        except Exception:
            continue
    return None


# ============================================================
# 手仕舞い条件
# ============================================================

@dataclass
class ExitRule:
    take_profit: float = 0.005     # min_profit_ratio
    hard_stop: float = -0.005      # hard_stop_ratio
    trailing: float = 0.003        # trailing_stop_ratio
    max_hold_min: float = 5.0      # max_hold_minutes
    unit_size: int = 100

    @classmethod
    def from_settings(cls, path: Optional[Path] = None) -> "ExitRule":
        p = path or (ROOT / "settings.ini")
        r = cls()
        try:
            cp = configparser.ConfigParser()
            cp.read(p, encoding="utf-8")
            if cp.has_section("exit"):
                s = cp["exit"]
                r.take_profit = float(s.get("min_profit_ratio", r.take_profit))
                r.hard_stop = float(s.get("hard_stop_ratio", r.hard_stop))
                r.trailing = float(s.get("trailing_stop_ratio", r.trailing))
                r.max_hold_min = float(s.get("max_hold_minutes", r.max_hold_min))
            if cp.has_section("trade"):
                r.unit_size = int(float(cp["trade"].get("unit_size", r.unit_size)))
        except Exception as e:  # noqa: BLE001
            print(f"  ! settings.ini を読めないので既定値を使う: {e}")
        return r


@dataclass
class TradeResult:
    symbol: str
    side: str
    entry_at: str
    entry_price: float
    exit_at: str
    exit_price: float
    exit_reason: str
    pnl_pct: float
    pnl_yen: float
    held_sec: float
    blocked_by: Optional[str]


def simulate(c: Candidate, ticks: List[Tuple[dt.datetime, float]], rule: ExitRule) -> Optional[TradeResult]:
    """候補地点で建てて、exit 条件で手仕舞う。"""
    # エントリーは「判断時刻以降の最初の tick」
    entry_idx = None
    for i, (t, _p) in enumerate(ticks):
        if t >= c.ts:
            entry_idx = i
            break
    if entry_idx is None:
        return None

    t0, entry_price = ticks[entry_idx]
    deadline = t0 + dt.timedelta(minutes=rule.max_hold_min)
    long = c.side == "BUY"
    best = entry_price          # 有利方向の最大値
    exit_price = entry_price
    exit_at = t0
    reason = "TIME"

    for t, p in ticks[entry_idx + 1:]:
        if t > deadline:
            break
        ret = (p - entry_price) / entry_price
        if not long:
            ret = -ret

        if ret >= rule.take_profit:
            exit_price, exit_at, reason = p, t, "TAKE_PROFIT"
            break
        if ret <= rule.hard_stop:
            exit_price, exit_at, reason = p, t, "HARD_STOP"
            break

        # トレーリング: 有利方向の最大値から trailing 分戻したら手仕舞い
        best = max(best, p) if long else min(best, p)
        peak_ret = (best - entry_price) / entry_price
        if not long:
            peak_ret = -peak_ret
        if peak_ret > 0:
            give_back = (best - p) / entry_price if long else (p - best) / entry_price
            if give_back >= rule.trailing:
                exit_price, exit_at, reason = p, t, "TRAILING"
                break

        exit_price, exit_at = p, t

    ret = (exit_price - entry_price) / entry_price
    if not long:
        ret = -ret
    return TradeResult(
        symbol=c.symbol, side=c.side,
        entry_at=t0.strftime("%H:%M:%S"), entry_price=entry_price,
        exit_at=exit_at.strftime("%H:%M:%S"), exit_price=exit_price,
        exit_reason=reason, pnl_pct=ret * 100.0,
        pnl_yen=(exit_price - entry_price) * (1 if long else -1) * rule.unit_size,
        held_sec=(exit_at - t0).total_seconds(),
        blocked_by=c.blocked_by,
    )


# ============================================================
# 集計と出力
# ============================================================

def summarize(trades: List[TradeResult]) -> Dict[str, Any]:
    if not trades:
        return {"trades": 0, "pnl_yen": 0.0, "win_rate": 0.0, "avg_pct": 0.0}
    wins = [t for t in trades if t.pnl_yen > 0]
    return {
        "trades": len(trades),
        "pnl_yen": round(sum(t.pnl_yen for t in trades), 1),
        "win_rate": round(len(wins) / len(trades) * 100.0, 1),
        "avg_pct": round(sum(t.pnl_pct for t in trades) / len(trades), 3),
        "best": round(max(t.pnl_yen for t in trades), 1),
        "worst": round(min(t.pnl_yen for t in trades), 1),
    }


def run(date_str: str, rule: ExitRule) -> Dict[str, Any]:
    print(f"=== 候補地点リプレイ {date_str} ===")
    cands = collect_candidates(date_str)
    print(f"候補地点: {len(cands)} 件")
    if not cands:
        return {"date": date_str, "candidates": 0}

    by_block: Dict[str, int] = {}
    for c in cands:
        by_block[c.blocked_by or "(発注まで到達)"] = by_block.get(c.blocked_by or "(発注まで到達)", 0) + 1
    for k, v in sorted(by_block.items(), key=lambda x: -x[1]):
        print(f"  {k:26s} {v:3d}")

    db = push_db_path_for(date_str)
    if not db:
        print(f"!! push DB が見つからない ({date_str})")
        return {"date": date_str, "candidates": len(cands), "error": "push db not found"}
    print(f"tick: {db}")

    ticks = load_ticks(db, [c.symbol for c in cands])
    have = sum(1 for s, v in ticks.items() if v)
    print(f"tick が取れた銘柄: {have}/{len(ticks)}")

    trades: List[TradeResult] = []
    no_tick: List[str] = []
    for c in cands:
        tk = ticks.get(c.symbol) or []
        r = simulate(c, tk, rule)
        if r is None:
            no_tick.append(c.symbol)
            continue
        trades.append(r)

    print(f"\n--- 全候補で建てた場合 ({len(trades)} 件) ---")
    _print_table(trades)
    overall = summarize(trades)
    print(f"  合計 {overall['pnl_yen']:+.0f}円 / 勝率 {overall['win_rate']}% / 平均 {overall['avg_pct']:+.3f}%")

    groups: Dict[str, List[TradeResult]] = {}
    for t in trades:
        groups.setdefault(t.blocked_by or "(発注まで到達)", []).append(t)

    print("\n--- ブロック要因ごとの成績 (そのフィルタを外していたら) ---")
    rows = []
    for k, v in sorted(groups.items(), key=lambda x: -len(x[1])):
        s = summarize(v)
        rows.append((k, s))
        print(f"  {k:26s} n={s['trades']:3d}  合計{s['pnl_yen']:+9.0f}円  勝率{s['win_rate']:5.1f}%  平均{s['avg_pct']:+.3f}%")

    if no_tick:
        print(f"\n  (tick が無く評価できなかった: {len(no_tick)} 件 {sorted(set(no_tick))[:8]})")

    return {
        "date": date_str,
        "candidates": len(cands),
        "evaluated": len(trades),
        "exit_rule": asdict(rule),
        "overall": overall,
        "by_blocker": {k: s for k, s in rows},
        "trades": [asdict(t) for t in trades],
    }


def _print_table(trades: List[TradeResult], limit: int = 20) -> None:
    if not trades:
        print("  (なし)")
        return
    print(f"  {'銘柄':<6}{'側':<5}{'entry':<9}{'exit':<9}{'理由':<13}{'損益%':>8}{'損益円':>9}  ブロック")
    for t in sorted(trades, key=lambda x: -abs(x.pnl_yen))[:limit]:
        print(f"  {t.symbol:<6}{t.side:<5}{t.entry_at:<9}{t.exit_at:<9}{t.exit_reason:<13}"
              f"{t.pnl_pct:+8.3f}{t.pnl_yen:+9.0f}  {t.blocked_by or '-'}")
    if len(trades) > limit:
        print(f"  ... 他 {len(trades)-limit} 件")


def sweep(date_str: str, base: ExitRule) -> None:
    """手仕舞い条件を振って比較する (ステップ3)。"""
    print("\n\n=== 手仕舞い条件のスイープ ===")
    cands = collect_candidates(date_str)
    db = push_db_path_for(date_str)
    if not cands or not db:
        print("  データが無いので実施しない")
        return
    ticks = load_ticks(db, [c.symbol for c in cands])

    tps = [0.003, 0.005, 0.008, 0.012]
    sls = [-0.003, -0.005, -0.008]
    holds = [3.0, 5.0, 10.0, 30.0]

    print(f"  {'利確':>6}{'損切':>7}{'保有分':>7}{'件数':>6}{'合計円':>10}{'勝率':>7}{'平均%':>8}")
    best: Optional[Tuple[float, Dict[str, Any], Tuple]] = None
    for tp in tps:
        for sl in sls:
            for hold in holds:
                r = ExitRule(take_profit=tp, hard_stop=sl, trailing=base.trailing,
                             max_hold_min=hold, unit_size=base.unit_size)
                trades = []
                for c in cands:
                    t = simulate(c, ticks.get(c.symbol) or [], r)
                    if t:
                        trades.append(t)
                s = summarize(trades)
                print(f"  {tp*100:5.1f}%{sl*100:6.1f}%{hold:7.0f}{s['trades']:6d}"
                      f"{s['pnl_yen']:10.0f}{s['win_rate']:6.1f}%{s['avg_pct']:+8.3f}")
                if best is None or s["pnl_yen"] > best[0]:
                    best = (s["pnl_yen"], s, (tp, sl, hold))
    if best:
        tp, sl, hold = best[2]
        print(f"\n  最良: 利確{tp*100:.1f}% 損切{sl*100:.1f}% 保有{hold:.0f}分 -> {best[0]:+.0f}円 "
              f"(勝率{best[1]['win_rate']}%)")
        print("  ※ 1 日 / 少数サンプルの結果。過剰に当てにしないこと。")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=dt.datetime.now().strftime("%Y%m%d"))
    ap.add_argument("--sweep", action="store_true", help="手仕舞い条件を振って比較する")
    ap.add_argument("--json", default="", help="結果を JSON で保存")
    args = ap.parse_args()

    rule = ExitRule.from_settings()
    print(f"手仕舞い条件 (settings.ini): 利確{rule.take_profit*100:.2f}% / "
          f"損切{rule.hard_stop*100:.2f}% / トレーリング{rule.trailing*100:.2f}% / "
          f"最大保有{rule.max_hold_min:.0f}分 / {rule.unit_size}株\n")

    out = run(args.date, rule)
    if args.sweep:
        sweep(args.date, rule)

    if args.json:
        Path(args.json).write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print(f"\nJSON: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
