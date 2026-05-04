# ============================================================
# batch/optimize_entry_thresholds.py
# ------------------------------------------------------------
# ✔ KPIに応じて ENTRY閾値を自動調整
# ✔ 人間介入ゼロ
# ============================================================

import json
import pandas as pd

LOG_PATH = "logs/entry_execution_log.csv"
CFG_PATH = "config/entry_thresholds.json"


def main():
    df = pd.read_csv(LOG_PATH)
    if df.empty:
        return

    df["profit_30s"] = df["price_30s"] > df["entry_price"]

    with open(CFG_PATH) as f:
        cfg = json.load(f)

    for mode, key, base in [
        ("BREAKOUT", "MIN_PROB_BREAKOUT", 0.65),
        ("PULLBACK", "MIN_PROB_PULLBACK", 0.70),
    ]:
        sub = df[df["entry_mode"] == mode]
        if sub.empty:
            continue

        rate = sub["profit_30s"].mean()

        if rate < base:
            cfg[key] = round(min(cfg[key] + 0.02, 0.90), 2)
            print(f"⬆ tighten {mode}: {cfg[key]}")
        elif rate > base + 0.05:
            cfg[key] = round(max(cfg[key] - 0.01, 0.50), 2)
            print(f"⬇ loosen {mode}: {cfg[key]}")

    with open(CFG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)

    print("✅ thresholds updated:", cfg)


if __name__ == "__main__":
    main()
