# ============================================================
# pred × pnl 簡易モニタ（直近N件）
# ============================================================

import pandas as pd
from pathlib import Path

LOG = Path("AI/logs/entry_exit_log.csv")

WINDOW = 50
MIN_CORR = 0.03

def check_pred_health():
    if not LOG.exists():
        return None

    df = pd.read_csv(LOG).tail(WINDOW)
    if len(df) < WINDOW:
        return None

    df["pnl"] = df["exit_price"] / df["entry_price"] - 1
    corr = df["ai_pred_1m"].corr(df["pnl"])

    return corr

if __name__ == "__main__":
    corr = check_pred_health()
    if corr is not None:
        print(f"[PRED_HEALTH] corr={corr:.4f}")
