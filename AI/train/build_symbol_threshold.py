# AI/train/build_symbol_threshold.py

import pandas as pd

df = pd.read_csv("AI/train/tosama_train.csv")

# 勝ちトレードのみ
win = df[df["label"] == 1]

rows = []

for symbol, g in win.groupby("symbol"):
    if len(g) < 20:
        continue  # データ不足は除外

    th = g["ai_confidence"].quantile(0.2)

    rows.append({
        "symbol": symbol,
        "ai_threshold": round(th, 3),
        "count": len(g),
    })

out = pd.DataFrame(rows)
out.to_csv("AI/config/symbol_ai_threshold.csv", index=False)
