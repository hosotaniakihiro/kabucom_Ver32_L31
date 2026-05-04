# ============================================================
# tools/kpi_mode_selection_analysis.py
# ------------------------------------------------------------
# ✔ 方式選択AIの正誤を可視化
# ✔ BREAKOUT / PULLBACK 別KPI
# ✔ 次の自動調整に使う
# ============================================================

import pandas as pd

LOG_PATH = "logs/entry_execution_log.csv"


def main():
    df = pd.read_csv(LOG_PATH)

    if df.empty:
        print("❌ log empty")
        return

    df["profit_30s"] = df["price_30s"] > df["entry_price"]

    print("\n=== MODE KPI ===")

    for mode in ["BREAKOUT", "PULLBACK"]:
        sub = df[df["entry_mode"] == mode]
        if sub.empty:
            continue

        rate = sub["profit_30s"].mean()
        count = len(sub)

        print(
            f"{mode:9s} "
            f"count={count:4d} "
            f"30s_profit_rate={rate:.2%}"
        )

    total_rate = df["profit_30s"].mean()
    print(f"\nTOTAL 30s profit rate: {total_rate:.2%}")


if __name__ == "__main__":
    main()
