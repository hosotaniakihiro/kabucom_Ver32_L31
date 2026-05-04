# AI/train/build_time_bias.py

import pandas as pd

df = pd.read_csv("AI/train/tosama_train.csv")

def bucket(sec):
    if sec < 600: return "OPEN_10"
    if sec < 1200: return "OPEN_20"
    if sec < 1800: return "OPEN_30"
    return "LATE"

df["bucket"] = df["entry_second"].apply(bucket)

rows = []

for (symbol, b), g in df.groupby(["symbol", "bucket"]):
    if len(g) < 15:
        continue

    win_rate = g["label"].mean()
    bias = (win_rate - 0.5) * 0.3   # ★ 効かせすぎ防止

    rows.append({
        "symbol": symbol,
        "bucket": b,
        "bias": round(bias, 3),
        "count": len(g),
    })

pd.DataFrame(rows).to_csv(
    "AI/config/symbol_time_bias.csv",
    index=False
)
