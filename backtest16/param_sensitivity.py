# ============================================================
# backtest16/param_sensitivity.py
# ------------------------------------------------------------
# §11 パラメータ感度分析(OFAT: 標準条件から1項目ずつ変える)。
#   - 最終評価期間は使わない(学習+検証のサンプル日のみ)。標準条件と分離。
#   - サンプル日の SymbolDay を一度だけ読み、パラメータ違いはメモリ内で再評価(高速)。
# 出力: reports/parameter_sensitivity.csv
# ============================================================

from __future__ import annotations

import datetime as dt
import os
from typing import List

import pandas as pd

from backtest16 import config as C, data_loader as DL, market_calendar as CAL, day_builder as DB
from backtest16.engine import signal_engine as SE, execution_engine as EE
from backtest16.analytics.metrics import strategy_metrics


def _sample_days(n: int) -> List[dt.date]:
    # 学習+検証期間のみ(最終評価は触らない §11)
    days = CAL.available_data_days(DL.C.SUMMARY_DIR, "summary", C.PERIOD_START, C.SPLIT_VALID_END,
                                   exclude_from=C.EXCLUDE_DATES_FROM)
    if n >= len(days):
        return days
    step = len(days) / n
    return [days[int(i * step)] for i in range(n)]


def _load_sample(n_days: int, symbols_limit: int) -> list:
    out = []
    for d in _sample_days(n_days):
        uni = DL.day_universe(d)[:symbols_limit] if symbols_limit else DL.day_universe(d)
        pv = DB.prev_trading_date(d)
        pc, ph = DB.prev_refs(pv, uni)
        days = DB.build_days(d, uni, pc, ph)
        out.append((d, list(days.values()), days))
    return out


def _run(params: dict, sample) -> List[dict]:
    trades = []
    for d, sds, dmap in sample:
        for sd in sds:
            for s in SE.detect_signals(sd, params):
                t = EE.execute(dmap[s.symbol], s, "C_OHLC", params)
                if t:
                    trades.append(t.as_row())
    return trades


def run(n_days: int = 8, symbols_limit: int = 150, out_dir: str = None) -> str:
    out_dir = out_dir or C.REPORTS_DIR
    os.makedirs(out_dir, exist_ok=True)
    sample = _load_sample(n_days, symbols_limit)
    rows = []

    def record(dim, val, trades):
        m = strategy_metrics(trades)
        rows.append({"param": dim, "value": val, "scope": "ALL",
                     "n": m.get("closed"), "win_rate": m.get("win_rate"),
                     "expectancy": m.get("expectancy"), "profit_factor": m.get("profit_factor"),
                     "net_profit": m.get("net_profit")})

    # 標準条件
    record("STANDARD", "default", _run({}, sample))
    # OFAT
    grid = {
        "take_profit_pct": C.PARAM_GRID["take_profit_pct"],
        "stop_loss_pct": C.PARAM_GRID["stop_loss_pct"],
        "max_hold_seconds": C.PARAM_GRID["max_hold_seconds"],
        "gap_up_pct": C.PARAM_GRID["gap_up_pct"],
        "gap_down_pct": C.PARAM_GRID["gap_down_pct"],
        "vwap_near_pct": C.PARAM_GRID["vwap_near_pct"],
        "volume_spike_mult": C.PARAM_GRID["volume_spike_mult"],
    }
    for dim, vals in grid.items():
        for v in vals:
            record(dim, v, _run({dim: v}, sample))

    df = pd.DataFrame(rows)
    path = os.path.join(out_dir, "parameter_sensitivity.csv")
    df.to_csv(path, index=False, encoding="utf-8-sig")
    return path


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=8)
    ap.add_argument("--symbols-limit", type=int, default=150)
    args = ap.parse_args()
    p = run(args.days, args.symbols_limit)
    print("[param_sensitivity] ->", p)
