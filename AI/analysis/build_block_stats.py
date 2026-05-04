import pandas as pd
import json

CSV = "AI/logs/ai_pass_log.csv"
OUT = "AI/config/block_cooldown_symbols.json"

BLOCK_LIMIT = 5

def main():
    df = pd.read_csv(CSV)

    blocks = (
        df[df["result"] == "BLOCK"]
        .groupby("symbol")
        .size()
        .reset_index(name="block_count")
    )

    cooldown = (
        blocks[blocks["block_count"] >= BLOCK_LIMIT]
        .set_index("symbol")["block_count"]
        .to_dict()
    )

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(cooldown, f, indent=2)

    print("❄ 冷却対象銘柄:", cooldown)

if __name__ == "__main__":
    main()
