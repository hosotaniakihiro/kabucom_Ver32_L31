# ============================================================
# backtest16/analytics/metrics.py
# ------------------------------------------------------------
# 戦略別の統計(§10)。実測/モデル値のみ。scipy非依存(Wilsonは自前)。
# 入力: Trade.as_row() の dict リスト(または DataFrame)。
# ============================================================

from __future__ import annotations

import math
from typing import List

import pandas as pd


def wilson_ci(wins: int, n: int, z: float = 1.96):
    """勝率の95% Wilson 信頼区間(scipy不要)。"""
    if n == 0:
        return (0.0, 0.0)
    p = wins / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = (z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / denom
    return (max(0.0, center - half), min(1.0, center + half))


def max_consecutive_losses(pnls: List[float]) -> int:
    mx = cur = 0
    for x in pnls:
        if x < 0:
            cur += 1; mx = max(mx, cur)
        else:
            cur = 0
    return mx


def max_drawdown(cum: List[float]) -> float:
    peak = -float("inf"); dd = 0.0
    for v in cum:
        peak = max(peak, v)
        dd = min(dd, v - peak)
    return dd  # 負値


def _bucket_winrate(df: pd.DataFrame, col: str) -> dict:
    out = {}
    if col not in df.columns:
        return out
    for key, g in df.groupby(col):
        n = len(g)
        w = int((g["net_pnl"] > 0).sum())
        out[str(key)] = {"n": n, "win_rate": (w / n if n else 0.0),
                         "net": float(g["net_pnl"].sum()), "avg": float(g["net_pnl"].mean()) if n else 0.0}
    return out


def strategy_metrics(trades: List[dict], *, signals_n: int = None, executable_n: int = None) -> dict:
    """1戦略(または1グループ)の統計一式。trades は filled のみでなく全件可(filledで集計)。"""
    df_all = pd.DataFrame(trades)
    if df_all.empty:
        return {"signals": signals_n or 0, "filled": 0, "closed": 0, "win_rate": 0.0,
                "net_profit": 0.0, "note": "no trades"}
    filled = df_all[df_all.get("filled") == True].copy() if "filled" in df_all.columns else df_all.copy()
    # 決済済み= filled かつ exit_price あり
    closed = filled[filled["exit_price"].notna()].copy() if "exit_price" in filled.columns else filled.copy()
    n = len(closed)
    pnls = closed["net_pnl"].astype(float).tolist() if n else []
    wins = int((closed["net_pnl"] > 0).sum()) if n else 0
    losses = int((closed["net_pnl"] < 0).sum()) if n else 0
    gross_profit = float(closed.loc[closed["net_pnl"] > 0, "net_pnl"].sum()) if n else 0.0
    gross_loss = float(closed.loc[closed["net_pnl"] < 0, "net_pnl"].sum()) if n else 0.0
    avg_win = float(closed.loc[closed["net_pnl"] > 0, "net_pnl"].mean()) if wins else 0.0
    avg_loss = float(closed.loc[closed["net_pnl"] < 0, "net_pnl"].mean()) if losses else 0.0
    net = float(closed["net_pnl"].sum()) if n else 0.0
    gross = float(closed["gross_pnl"].sum()) if n and "gross_pnl" in closed else 0.0
    pf = (gross_profit / abs(gross_loss)) if gross_loss < 0 else (float("inf") if gross_profit > 0 else 0.0)
    wr = wins / n if n else 0.0
    ci = wilson_ci(wins, n)
    cum = []
    s = 0.0
    for x in pnls:
        s += x; cum.append(s)
    exit_reasons = closed["exit_reason"].value_counts().to_dict() if n else {}

    # 銘柄別寄与・日別/月別
    by_symbol = {}
    if n and "symbol" in closed:
        by_symbol = closed.groupby("symbol")["net_pnl"].sum().sort_values().to_dict()
    daily = {}
    monthly = {}
    if n and "date" in closed:
        closed["_d"] = closed["date"].astype(str)
        daily = closed.groupby("_d")["net_pnl"].sum().to_dict()
        closed["_m"] = closed["_d"].str.slice(0, 7)
        monthly = closed.groupby("_m")["net_pnl"].sum().to_dict()

    # 寄与集中度の警告(上位3銘柄 or 上位3日で純益の大半)
    conc_warn = None
    if net > 0 and by_symbol:
        top3 = sum(sorted(by_symbol.values(), reverse=True)[:3])
        if top3 >= 0.8 * net:
            conc_warn = "純益の8割超が上位3銘柄に依存"
    if net > 0 and daily:
        pos_days = [v for v in daily.values() if v > 0]
        top3d = sum(sorted(daily.values(), reverse=True)[:3])
        if top3d >= 0.8 * net:
            conc_warn = (conc_warn + " / " if conc_warn else "") + "純益の8割超が上位3営業日に依存"

    tp = int((closed["exit_reason"] == "TP").sum()) if n else 0
    sl = int((closed["exit_reason"] == "SL").sum()) if n else 0
    tm = int((closed["exit_reason"] == "TIME").sum()) if n else 0

    return {
        "signals": signals_n if signals_n is not None else len(df_all),
        "executable": executable_n,
        "filled": int(len(filled)),
        "unfilled": int((df_all.get("filled") == False).sum()) if "filled" in df_all else 0,
        "closed": n,
        "win_rate": wr,
        "win_rate_ci95": [round(ci[0], 4), round(ci[1], 4)],
        "avg_win": round(avg_win, 1),
        "avg_loss": round(avg_loss, 1),
        "expectancy": round(net / n, 1) if n else 0.0,
        "profit_factor": (round(pf, 3) if pf != float("inf") else "inf"),
        "gross_profit": round(gross_profit, 0),
        "gross_loss": round(gross_loss, 0),
        "net_profit": round(net, 0),
        "gross_pnl_sum": round(gross, 0),
        "max_consecutive_losses": max_consecutive_losses(pnls),
        "max_drawdown": round(max_drawdown(cum), 0),
        "avg_hold_seconds": round(float(closed["hold_seconds"].mean()), 1) if n and "hold_seconds" in closed else 0.0,
        "tp_rate": round(tp / n, 4) if n else 0.0,
        "sl_rate": round(sl / n, 4) if n else 0.0,
        "time_rate": round(tm / n, 4) if n else 0.0,
        "avg_mae_pct": round(float(closed["mae_pct"].mean()), 4) if n and "mae_pct" in closed else 0.0,
        "avg_mfe_pct": round(float(closed["mfe_pct"].mean()), 4) if n and "mfe_pct" in closed else 0.0,
        "exit_reasons": exit_reasons,
        "by_hour": _bucket_winrate(closed, "entry_hour") if n else {},
        "by_gap_bucket": _bucket_winrate(closed, "gap_bucket") if n else {},
        "by_vwap_side": _bucket_winrate(closed, "vwap_side") if n else {},
        "by_exec_mode": _bucket_winrate(closed, "exec_mode") if n else {},
        "daily_pnl": {k: round(v, 0) for k, v in daily.items()},
        "monthly_pnl": {k: round(v, 0) for k, v in monthly.items()},
        "top_symbol_contrib": {k: round(v, 0) for k, v in list(sorted(by_symbol.items(), key=lambda kv: kv[1]))[:5]},
        "concentration_warning": conc_warn,
    }
