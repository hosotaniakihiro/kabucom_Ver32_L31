# ============================================================
# AI/build_risk_daily_stats.py
# ============================================================

import pandas as pd

RISK_LOG = "logs/risk_ai_log.csv"
TRADE_LOG = "logs/trade_history.csv"   # ← 既存ログを想定
OUT_CSV = "logs/risk_daily_stats.csv"


def main():

    risk = pd.read_csv(RISK_LOG, parse_dates=["datetime"])
    trade = pd.read_csv(TRADE_LOG, parse_dates=["trade_time"])

    risk["date"] = risk["datetime"].dt.date
    trade["date"] = trade["trade_time"].dt.date

    # --- 日次 STOP 回数 ---
    stop_cnt = (
        risk[risk["event"].isin(["STOP", "FORCE_STOP"])]
        .groupby("date")
        .size()
        .rename("stop_count")
    )

    # --- 日次損益 ---
    pnl = (
        trade.groupby("date")["pnl"]
        .sum()
        .rename("total_pnl")
    )

    # --- 日中最大 DD ---
    trade["cum_pnl"] = trade.groupby("date")["pnl"].cumsum()
    trade["dd"] = trade["cum_pnl"] - trade.groupby("date")["cum_pnl"].cummax()
    max_dd = (
        trade.groupby("date")["dd"]
        .min()
        .rename("max_dd")
    )

    # --- 取引回数 ---
    cnt = (
        trade.groupby("date")
        .size()
        .rename("trade_count")
    )

    df = pd.concat([pnl, max_dd, cnt, stop_cnt], axis=1).fillna(0)
    df.to_csv(OUT_CSV)

    print(f"saved: {OUT_CSV}")
    print(df.tail())


if __name__ == "__main__":
    main()
