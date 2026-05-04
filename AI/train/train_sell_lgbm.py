# ============================================================
# AI/train/train_sell_lgbm.py
# ------------------------------------------------------------
# ✔ SELL 専用 LGBM
# ✔ ラベル反転
# ============================================================

import lightgbm as lgb
import pandas as pd
from pathlib import Path

CSV = Path("AI/train/train_1m.csv")
MODEL_OUT = Path("AI/models/sell_lgbm_1m.pkl")

df = pd.read_csv(CSV)

FEATURES = ["ret", "body", "range", "vol_ratio", "fast_ret"]
X = df[FEATURES]
y = -df["y"]   # ★ラベル反転

train = lgb.Dataset(X, y)

params = dict(
    objective="regression",
    learning_rate=0.05,
    num_leaves=31,
)

model = lgb.train(params, train, num_boost_round=200)
MODEL_OUT.parent.mkdir(parents=True, exist_ok=True)
model.save_model(str(MODEL_OUT))

print("✅ SELL model trained")
