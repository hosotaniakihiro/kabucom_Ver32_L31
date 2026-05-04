# ============================================================
# AI/analysis/health_check.py
# ------------------------------------------------------------
# ✔ pred × pnl 劣化検知
# ✔ RiskAI 停止シグナル生成
# ============================================================

import pandas as pd
from pathlib import Path

LOG_PATH = Path("AI/logs/entry_exit_log.csv")

MIN_TRADES = 200
MIN_CORR = 0.05     # これ未満なら壊れたと判断

def main():
    df = pd.read_csv(LOG_PATH)
    df = df.dropna(subset=["ai_pred_1m", "entry_price", "exit_price"])

    if len(df) < MIN_TRADES:
        print("⚠ not enough trades")
        return

    df["pnl"] = df["exit_price"] / df["entry_price"] - 1
    corr = df["ai_pred_1m"].corr(df["pnl"])

    print(f"[HEALTH] pred-pnl corr = {corr:.4f}")

    if corr < MIN_CORR:
        Path("AI/state/ai_stop.flag").parent.mkdir(exist_ok=True)
        Path("AI/state/ai_stop.flag").write_text("STOP")
        print("⛔ AI STOP FLAG CREATED")
    else:
        flag = Path("AI/state/ai_stop.flag")
        if flag.exists():
            flag.unlink()
            print("✅ AI STOP FLAG CLEARED")

if __name__ == "__main__":
    main()
