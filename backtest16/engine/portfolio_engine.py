# ============================================================
# backtest16/engine/portfolio_engine.py
# ------------------------------------------------------------
# 統合ポートフォリオ(§9D)。単一戦略の仮想結果(資金競合なし)とは別に、
# 16戦略を同じ仮想ポートフォリオで走らせたときの結果を出す。
#   - 1銘柄70万(数量は約定時に担保済み)
#   - 最大同時建玉3(買い+売り 合計)
#   - 同一銘柄の買いと空売りの同時建玉は標準で禁止
#   - 同時シグナルは事前固定の優先順位(config.SIGNAL_PRIORITY)
#   - 約定できなかったシグナルを記録
# 各 trade は単一戦略シミュレーションの entry_ts/exit_ts/net_pnl を用いる
# (資金競合はスロット占有で表現)。
# ============================================================

from __future__ import annotations

from typing import List

import pandas as pd

from backtest16 import config as C


def _priority(sid: str) -> int:
    try:
        return C.SIGNAL_PRIORITY.index(sid)
    except ValueError:
        return len(C.SIGNAL_PRIORITY) + 1


def simulate(trades: List[dict], mode: str = "both",
             max_concurrent: int = None, allow_both_sides_same_symbol: bool = None) -> dict:
    """mode: 'long'|'short'|'both'。trades は filled かつ entry/exit 時刻ありの dict。"""
    max_concurrent = max_concurrent or C.MAX_CONCURRENT_POSITIONS
    if allow_both_sides_same_symbol is None:
        allow_both_sides_same_symbol = C.ALLOW_SIMULTANEOUS_LONG_SHORT_SAME_SYMBOL

    rows = [t for t in trades if t.get("filled") and t.get("entry_ts") and t.get("exit_ts")]
    if mode == "long":
        rows = [t for t in rows if t["side"] == "LONG"]
    elif mode == "short":
        rows = [t for t in rows if t["side"] == "SHORT"]
    # 時刻順(同時刻は優先順位)
    rows.sort(key=lambda t: (t["entry_ts"], _priority(t["strategy_id"])))

    open_pos = []   # list of dict(exit_ts, symbol, side)
    taken, missed = [], []
    for t in rows:
        ent = pd.Timestamp(t["entry_ts"]); ext = pd.Timestamp(t["exit_ts"])
        # 期限切れポジションを解放
        open_pos = [p for p in open_pos if p["exit_ts"] > ent]
        sym = t["symbol"]; side = t["side"]
        # 同一銘柄 両サイド禁止
        if not allow_both_sides_same_symbol and any(p["symbol"] == sym and p["side"] != side for p in open_pos):
            t2 = dict(t); t2["miss_reason"] = "SAME_SYMBOL_OPPOSITE"; missed.append(t2); continue
        # 同一銘柄同サイドの重複は1本に
        if any(p["symbol"] == sym and p["side"] == side for p in open_pos):
            t2 = dict(t); t2["miss_reason"] = "ALREADY_OPEN"; missed.append(t2); continue
        if len(open_pos) >= max_concurrent:
            t2 = dict(t); t2["miss_reason"] = "MAX_CONCURRENT"; missed.append(t2); continue
        open_pos.append({"exit_ts": ext, "symbol": sym, "side": side})
        taken.append(t)

    net = sum(float(t["net_pnl"]) for t in taken)
    wins = sum(1 for t in taken if float(t["net_pnl"]) > 0)
    n = len(taken)
    # 日別純益
    daily = {}
    for t in taken:
        dkey = str(t["date"])
        daily[dkey] = daily.get(dkey, 0.0) + float(t["net_pnl"])
    return {
        "mode": mode,
        "taken": n,
        "missed": len(missed),
        "missed_reasons": pd.Series([m["miss_reason"] for m in missed]).value_counts().to_dict() if missed else {},
        "win_rate": (wins / n) if n else 0.0,
        "net_profit": round(net, 0),
        "avg_net": round(net / n, 1) if n else 0.0,
        "daily_pnl": {k: round(v, 0) for k, v in daily.items()},
    }
