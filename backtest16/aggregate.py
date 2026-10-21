# ============================================================
# backtest16/aggregate.py
# ------------------------------------------------------------
# 日次チェックポイントを集計して成果物(CSV/JSON)と分析結果を作る(§10/§12/§18)。
#   - 標準条件(config既定)の結果のみを集計(最適化とは分離 §11)。
#   - A/B/C を混ぜて単一勝率にしない(§8): exec_mode 別に集計。
#   - 空売りの実損益は区分Aが出せないため「仮想」。borrow区分を併記(§6.1)。
# ============================================================

from __future__ import annotations

import datetime as dt
import glob
import json
import os
from typing import List

import pandas as pd

from backtest16 import config as C
from backtest16 import borrow_availability as BA
from backtest16.strategies import all_strategies
from backtest16.analytics import metrics as M
from backtest16.analytics import first_five_minutes as F5A
from backtest16.analytics import long_short_comparison as LS
from backtest16.analytics import walk_forward as WF
from backtest16.engine import portfolio_engine as PE


def load_checkpoints() -> dict:
    trades, first5, day_meta = [], [], []
    for p in sorted(glob.glob(os.path.join(C.CHECKPOINT_DIR, "day_*.json"))):
        try:
            with open(p, encoding="utf-8") as f:
                r = json.load(f)
        except Exception:
            continue
        trades.extend(r.get("trades", []))
        first5.extend(r.get("first5", []))
        day_meta.append({k: r.get(k) for k in
                         ("date", "prev_date", "universe", "symbols_with_bars",
                          "symbols_with_prevclose", "symbols_with_ticks_for_signals",
                          "signal_symbols", "signals", "elapsed_sec")})
    return {"trades": trades, "first5": first5, "day_meta": day_meta}


def _c_trades(trades: List[dict]) -> List[dict]:
    return [t for t in trades if t.get("exec_mode") == "C_OHLC"]


def build(out_dir: str = None) -> dict:
    out_dir = out_dir or C.REPORTS_DIR
    os.makedirs(out_dir, exist_ok=True)
    data = load_checkpoints()
    trades = data["trades"]
    strat_meta = {sid: {"side": s.side, "name": s.name} for sid, s in all_strategies().items()}

    df = pd.DataFrame(trades)
    if not df.empty:
        df.to_csv(os.path.join(out_dir, "trades.csv"), index=False, encoding="utf-8-sig")

    # ---- 戦略別メトリクス(exec_mode別 + C主) ----
    strategy_summary_rows = []
    per_strategy = {}
    c_all = _c_trades(trades)
    for sid, meta in strat_meta.items():
        s_c = [t for t in c_all if t["strategy_id"] == sid]
        m_c = M.strategy_metrics(s_c)
        per_mode = {}
        for mode in C.EXECUTION_MODES:
            sm = [t for t in trades if t["strategy_id"] == sid and t["exec_mode"] == mode]
            if sm:
                per_mode[mode] = M.strategy_metrics(sm)
        wf = WF.split_metrics(s_c)
        per_strategy[sid] = {"side": meta["side"], "name": meta["name"],
                             "C_OHLC": m_c, "by_mode": per_mode, "walk_forward": wf}
        strategy_summary_rows.append({
            "id": sid, "side": meta["side"], "name": meta["name"],
            "signals": m_c.get("signals"), "filled": m_c.get("filled"), "closed": m_c.get("closed"),
            "win_rate": m_c.get("win_rate"), "expectancy": m_c.get("expectancy"),
            "profit_factor": m_c.get("profit_factor"), "net_profit": m_c.get("net_profit"),
            "max_dd": m_c.get("max_drawdown"), "wf_consistency": wf.get("consistency"),
        })
    ss = pd.DataFrame(strategy_summary_rows)
    if not ss.empty:
        ss.to_csv(os.path.join(out_dir, "strategy_summary.csv"), index=False, encoding="utf-8-sig")

    # ---- 日別純益(C主) ----
    daily_rows = []
    if c_all:
        cdf = pd.DataFrame([t for t in c_all if t.get("filled") and t.get("exit_price") is not None])
        if not cdf.empty:
            cdf["date"] = cdf["date"].astype(str)
            for (d, side), g in cdf.groupby(["date", "side"]):
                daily_rows.append({"date": d, "side": side, "net_pnl": round(float(g["net_pnl"].sum()), 0),
                                   "trades": len(g), "win_rate": round(float((g["net_pnl"] > 0).mean()), 4)})
    pd.DataFrame(daily_rows).to_csv(os.path.join(out_dir, "daily_pnl.csv"), index=False, encoding="utf-8-sig")

    # ---- 除外/未約定シグナル ----
    excluded = [t for t in trades if not t.get("filled")]
    pd.DataFrame(excluded).to_csv(os.path.join(out_dir, "excluded_signals.csv"), index=False, encoding="utf-8-sig")

    # ---- 空売り制約(borrow区分) ----
    short_rows = []
    for t in c_all:
        if t["side"] != "SHORT":
            continue
        klass = BA.classify(t["symbol"], dt.date.fromisoformat(str(t["date"])[:10]))
        short_rows.append({"date": t["date"], "symbol": t["symbol"], "strategy_id": t["strategy_id"],
                           "borrow_class": klass, "executable": BA.is_executable_class(klass)})
    sc = pd.DataFrame(short_rows)
    if not sc.empty:
        sc.to_csv(os.path.join(out_dir, "short_sale_constraints.csv"), index=False, encoding="utf-8-sig")
    borrow_counts = sc["borrow_class"].value_counts().to_dict() if not sc.empty else {}

    # ---- ポートフォリオ3通り ----
    portfolio = {m: PE.simulate(c_all, mode=m) for m in ("long", "short", "both")}

    # ---- 初動5分足(§13) ----
    first5_agg = F5A.aggregate(data["first5"])

    # ---- 買いVS空売り(§9C) ----
    long_short = LS.compare(trades)

    # ---- データ品質(§7/§3) ----
    dm = pd.DataFrame(data["day_meta"])
    data_quality = {
        "days_processed": int(len(dm)),
        "date_min": dm["date"].min() if not dm.empty else None,
        "date_max": dm["date"].max() if not dm.empty else None,
        "avg_symbols_with_bars": round(float(dm["symbols_with_bars"].mean()), 1) if not dm.empty else 0,
        "avg_universe": round(float(dm["universe"].mean()), 1) if not dm.empty else 0,
        "total_signals": int(dm["signals"].sum()) if not dm.empty else 0,
        "avg_tick_symbols_per_day": round(float(dm["symbols_with_ticks_for_signals"].mean()), 1) if not dm.empty else 0,
        "first5_records": len(data["first5"]),
        "notes": [
            "約定はA/B/C分離。主たる執行可能モードはC_OHLC(確定1分足)。",
            "push板ティック(A/B)はシグナル銘柄のごく一部のみ(当日の実データが疎)。",
            "空売りの売建可否はborrow区分B/Dが中心→空売り実損益は仮想(区分A不可)。",
            BA.data_availability_note(),
        ],
    }

    summary = {
        "generated_at": None,   # 外部から stamp
        "config": {
            "period": [C.PERIOD_START.isoformat(), C.PERIOD_END.isoformat()],
            "take_profit_pct": C.TAKE_PROFIT_PCT, "stop_loss_pct": C.STOP_LOSS_PCT,
            "max_hold_seconds": C.MAX_HOLD_SECONDS, "per_symbol_notional": C.PER_SYMBOL_NOTIONAL_YEN,
            "max_concurrent": C.MAX_CONCURRENT_POSITIONS,
            "split": {"train_end": C.SPLIT_TRAIN_END.isoformat(), "valid_end": C.SPLIT_VALID_END.isoformat()},
            "cost": C.COST_ASSUMPTIONS,
        },
        "data_quality": data_quality,
        "strategies": per_strategy,
        "strategy_summary": strategy_summary_rows,
        "portfolio": portfolio,
        "borrow_class_counts": borrow_counts,
        "first5_analysis": first5_agg,
        "long_vs_short": long_short,
    }
    with open(os.path.join(out_dir, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1, default=str)
    return summary


if __name__ == "__main__":
    s = build()
    print("[aggregate] days=", s["data_quality"]["days_processed"],
          "signals=", s["data_quality"]["total_signals"])
