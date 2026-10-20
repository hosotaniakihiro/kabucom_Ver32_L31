# ============================================================
# backtest16/run.py
# ------------------------------------------------------------
# 16戦略バックテストのオーケストレータ(§17)。
#   - 営業日は「実在する summary DB の日付」をground truthに(§3/§7)。
#   - 日次チェックポイントで再開可能(§17: 長時間処理対策)。
#   - 既存DBは読み取り専用。実発注・本番変更は一切しない。
#
# 使い方(ソースコード/ から):
#   .venv/Scripts/python.exe -m backtest16.run --start 2026-04-20 --end 2026-10-20
#   .venv/Scripts/python.exe -m backtest16.run --sample 12        # 窓内から均等12日
#   .venv/Scripts/python.exe -m backtest16.run --days 2026-06-16  # 単日
# 出力: backtest16/data/checkpoints/day_YYYYMMDD.json, reports/trades.csv ほかは aggregate.py
# ============================================================

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import time
from typing import List

from backtest16 import config as C
from backtest16 import data_loader as DL
from backtest16 import market_calendar as CAL
from backtest16 import day_builder as DB
from backtest16.engine import signal_engine as SE, execution_engine as EE
from backtest16.analytics import first_five_minutes as F5

_UNI_CACHE = {}


def _universe(d: dt.date, limit: int = None) -> List[str]:
    key = d.isoformat()
    if key not in _UNI_CACHE:
        _UNI_CACHE[key] = DL.day_universe(d)
    u = _UNI_CACHE[key]
    return u[:limit] if limit else u


def _ckpt_path(d: dt.date) -> str:
    os.makedirs(C.CHECKPOINT_DIR, exist_ok=True)
    return os.path.join(C.CHECKPOINT_DIR, f"day_{d:%Y%m%d}.json")


def process_day(d: dt.date, symbols_limit: int = None, modes=("C_OHLC",)) -> dict:
    t0 = time.time()
    uni = _universe(d, symbols_limit)
    prev_d = DB.prev_trading_date(d)
    pc, ph = DB.prev_refs(prev_d, uni)
    days = DB.build_days(d, uni, pc, ph)
    params = {}

    signals = []
    for sym, sd in days.items():
        signals.extend(SE.detect_signals(sd, params))

    trades = []
    # C_OHLC: 全シグナル(主たる執行可能モード)
    for s in signals:
        t = EE.execute(days[s.symbol], s, "C_OHLC", params)
        if t:
            trades.append(t.as_row())

    # ティックのある銘柄のみ B/A
    sig_syms = sorted(set(s.symbol for s in signals))
    n_tick = DB.attach_ticks(days, d, sig_syms) if sig_syms else 0
    for s in signals:
        if days[s.symbol].ticks is not None and not days[s.symbol].ticks.empty:
            for mode in ("B_PUSH", "A_BOARD"):
                t = EE.execute(days[s.symbol], s, mode, params)
                if t:
                    trades.append(t.as_row())

    # §13 初動5分足分析(全銘柄日・戦略非依存)。ティック後付け済みなら利用。
    f5 = [F5.analyze_symbol_day(sd) for sd in days.values()]
    f5 = [r for r in f5 if r]

    result = {
        "date": d.isoformat(),
        "prev_date": prev_d.isoformat() if prev_d else None,
        "universe": len(uni),
        "symbols_with_bars": len(days),
        "symbols_with_prevclose": len(pc),
        "symbols_with_ticks_for_signals": n_tick,
        "signal_symbols": len(sig_syms),
        "signals": len(signals),
        "trades": trades,
        "first5": f5,
        "elapsed_sec": round(time.time() - t0, 1),
    }
    with open(_ckpt_path(d), "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False)
    return result


def select_days(start: dt.date, end: dt.date, sample: int = None, only=None) -> List[dt.date]:
    if only:
        return only
    days = CAL.available_data_days(DL.C.SUMMARY_DIR, "summary", start, end,
                                   exclude_from=C.EXCLUDE_DATES_FROM)
    if sample and sample < len(days):
        step = len(days) / sample
        return [days[int(i * step)] for i in range(sample)]
    return days


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=C.PERIOD_START.isoformat())
    ap.add_argument("--end", default=C.PERIOD_END.isoformat())
    ap.add_argument("--sample", type=int, default=None, help="窓内から均等サンプルする日数")
    ap.add_argument("--days", default=None, help="単日/カンマ区切り日付 YYYY-MM-DD")
    ap.add_argument("--symbols-limit", type=int, default=None)
    ap.add_argument("--resume", action="store_true", help="チェックポイント済みの日はスキップ")
    args = ap.parse_args()

    start = dt.date.fromisoformat(args.start); end = dt.date.fromisoformat(args.end)
    only = None
    if args.days:
        only = [dt.date.fromisoformat(x) for x in args.days.split(",")]
    days = select_days(start, end, args.sample, only)
    print(f"[run] {len(days)} days: {days[0]}..{days[-1]}" if days else "[run] no days")

    done = 0
    for d in days:
        if args.resume and os.path.exists(_ckpt_path(d)):
            print(f"[skip] {d} (checkpoint exists)")
            continue
        try:
            r = process_day(d, args.symbols_limit)
            print(f"[day] {d} syms={r['symbols_with_bars']} sig={r['signals']} "
                  f"trades={len(r['trades'])} ticks={r['symbols_with_ticks_for_signals']} "
                  f"f5={len(r['first5'])} {r['elapsed_sec']}s")
            done += 1
        except Exception as e:
            print(f"[ERR] {d}: {type(e).__name__} {e}")
    print(f"[run] done {done}/{len(days)} days. checkpoints in {C.CHECKPOINT_DIR}")


if __name__ == "__main__":
    main()
