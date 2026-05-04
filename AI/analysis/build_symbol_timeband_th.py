import pandas as pd
import json
from collections import defaultdict

CSV = "AI/logs/ai_pass_log.csv"
OUT = "AI/config/symbol_timeband_th.json"

TARGET_PASS = 0.3
MIN_SAMPLES = 20

def main():
    df = pd.read_csv(CSV)
    df = df[df["stage"] == "final_ai"]

    table = defaultdict(dict)

    for (symbol, hour), g in df.groupby(["symbol", "hour"]):
        if len(g) < MIN_SAMPLES:
            continue

        g = g.sort_values("confidence", ascending=False)
        idx = max(1, int(len(g) * TARGET_PASS))
        th = float(g.iloc[idx]["confidence"])
        table[symbol][str(hour)] = round(th, 3)

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(table, f, indent=2, ensure_ascii=False)

    print("✅ symbol × timeband threshold generated")

if __name__ == "__main__":
    main()
