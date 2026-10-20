# ============================================================
# backtest16/analytics/long_short_comparison.py
# ------------------------------------------------------------
# §9C 買い VS 空売り。同一銘柄・同一日の条件別に、買いと空売りの優劣を比較する。
# 取引単位だけでなく銘柄日単位でも集計(相互依存の考慮 §9C)。
# 入力は C_OHLC(主たる執行可能モード)のトレード。
# ============================================================

from __future__ import annotations

from typing import List

import pandas as pd


def _grp_stats(g: pd.DataFrame) -> dict:
    n = len(g)
    if n == 0:
        return {"n": 0, "win_rate": None, "expectancy": None, "net": 0.0}
    return {"n": n, "win_rate": round(float((g["net_pnl"] > 0).mean()), 4),
            "expectancy": round(float(g["net_pnl"].mean()), 1),
            "net": round(float(g["net_pnl"].sum()), 0)}


def compare(trades: List[dict]) -> dict:
    df = pd.DataFrame([t for t in trades if t.get("filled") and t.get("exit_price") is not None])
    if df.empty:
        return {"n": 0}
    df = df[df["exec_mode"] == "C_OHLC"].copy()
    if df.empty:
        return {"n": 0}

    def by(col):
        res = {}
        for key, g in df.groupby(col):
            res[str(key)] = {"LONG": _grp_stats(g[g["side"] == "LONG"]),
                             "SHORT": _grp_stats(g[g["side"] == "SHORT"])}
        return res

    # 時間帯(9:05-9:15 / 9:15-9:30 / 9:30以降)
    def hourwin(ts):
        t = pd.Timestamp(ts).time()
        import datetime as dt
        if t < dt.time(9, 15):
            return "0905-0915"
        if t < dt.time(9, 30):
            return "0915-0930"
        return "0930+"
    df["entry_win"] = df["entry_ts"].map(hourwin)

    out = {
        "n": int(len(df)),
        "overall": {"LONG": _grp_stats(df[df["side"] == "LONG"]),
                    "SHORT": _grp_stats(df[df["side"] == "SHORT"])},
        "by_gap_bucket": by("gap_bucket"),
        "by_first5_dir": {str(k): {"LONG": _grp_stats(g[g["side"] == "LONG"]),
                                   "SHORT": _grp_stats(g[g["side"] == "SHORT"])}
                          for k, g in df.groupby(df["first5_bullish"].map({True: "陽線", False: "陰線"}))},
        "by_entry_window": by("entry_win"),
        "by_vwap_side": by("vwap_side"),
    }
    # 銘柄日単位(同一銘柄日で買い/売りどちらが勝ったか)
    sd = df.groupby(["date", "symbol", "side"])["net_pnl"].sum().reset_index()
    piv = sd.pivot_table(index=["date", "symbol"], columns="side", values="net_pnl", aggfunc="sum")
    both = piv.dropna(subset=[c for c in ("LONG", "SHORT") if c in piv.columns], how="any") if ("LONG" in piv.columns and "SHORT" in piv.columns) else pd.DataFrame()
    if not both.empty:
        out["symbol_day_both_sides"] = {
            "n": int(len(both)),
            "long_better_rate": round(float((both["LONG"] > both["SHORT"]).mean()), 4),
        }
    return out
