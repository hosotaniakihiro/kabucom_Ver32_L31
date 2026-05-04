import pandas as pd
import json

CSV = "AI/logs/ai_pass_log.csv"
OUT = "AI/config/market_confidence.json"

def main():
    df = pd.read_csv(CSV)
    today = df.tail(500)

    pass_rate = (
        (today["result"] == "PASS").sum() / len(today)
    )

    data = {
        "pass_rate": round(pass_rate, 3),
        "allow_trade": pass_rate >= 0.2,
    }

    with open(OUT, "w") as f:
        json.dump(data, f, indent=2)

    print("🧠 market_confidence:", data)

if __name__ == "__main__":
    main()
