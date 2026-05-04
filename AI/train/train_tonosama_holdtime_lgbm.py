# ============================================================
# pj/AI/train/train_tonosama_holdtime_lgbm.py
# 2025-12-31
# ------------------------------------------------------------
# TONOSAMA 最適 holding 秒数 回帰モデル
# ============================================================

import pandas as pd
import lightgbm as lgb
import joblib
from sklearn.model_selection import train_test_split

CSV = "AI/train/tosama_holdtime.csv"
MODEL_OUT = "AI/model/tonosama_holdtime_lgbm.pkl"

FEATURES = [
    "volume_speed",
    "fast_ret",
    "rank_position",
    "price",
    "spread",
    "entry_second",
]

df = pd.read_csv(CSV)

X = df[FEATURES]
y = df["hold_seconds"]

X_train, X_valid, y_train, y_valid = train_test_split(
    X, y, test_size=0.2, random_state=42
)

model = lgb.LGBMRegressor(
    objective="regression",
    n_estimators=300,
    learning_rate=0.05,
    num_leaves=31,
    max_depth=-1,
    subsample=0.8,
    colsample_bytree=0.8,
)

model.fit(
    X_train, y_train,
    eval_set=[(X_valid, y_valid)],
    eval_metric="l1",
    verbose=50,
)

joblib.dump(model, MODEL_OUT)
print(f"[MODEL SAVED] {MODEL_OUT}")
