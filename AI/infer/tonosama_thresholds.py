# pj/AI/infer/tonosama_thresholds.py
import json, pandas as pd

CSV = "AI/train/tosama_train.csv"
OUT = "AI/models/tonosama_thresholds.json"

df = pd.read_csv(CSV)
df["hour"] = df["entry_second"] // 3600

thresholds = {}

for (sym, hour), g in df.groupby(["symbol", "hour"]):
    win = g[g["label"] == 1]
    if len(win) < 10:
        continue
    thresholds.setdefault(sym, {})[str(hour)] = {
        "ai_conf": win["ai_confidence"].quantile(0.3),
        "fast_ret": win["fast_ret"].quantile(0.3),
    }

json.dump(thresholds, open(OUT, "w"), indent=2)
