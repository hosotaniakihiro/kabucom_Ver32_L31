# ============================================================
# AI/risk/risk_guard.py
# ------------------------------------------------------------
# ✔ pred × pnl 相関 / EV 劣化で ENTRY 停止
# ============================================================

import pandas as pd
from pathlib import Path

LOG = Path("AI/logs/entry_exit_log.csv")

MIN_TRADES = 50
MIN_CORR = 0.00
MIN_EV = 0.0

def risk_ok():
    if not LOG.exists():
        return True

    df = pd.read_csv(LOG).tail(200)
    if len(df) < MIN_TRADES:
        return True

    if "ai_pred_1m" not in df:
        return True

    df["pnl"] = df["exit_price"] / df["entry_price"] - 1
    corr = df["ai_pred_1m"].corr(df["pnl"])
    ev = df["pnl"].mean()

    if corr < MIN_CORR or ev < MIN_EV:
        return False
    return True
