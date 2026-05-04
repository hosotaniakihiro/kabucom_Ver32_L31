# pj/AI/train/train_tonosama_lgbm.py
import json, joblib
import pandas as pd
import lightgbm as lgb
from sklearn.model_selection import train_test_split
from sklearn.metrics import roc_auc_score

CSV_PATH = "AI/train/tosama_train.csv"
MODEL_PATH = "AI/models/tonosama_lgbm.pkl"
FEATURE_PATH = "AI/models/tonosama_features.json"

FEATURES = [
    "volume_speed",
    "fast_ret",
    "rank_position",
    "price",
    "spread",
    "entry_second",
]

df = pd.read_csv(CSV_PATH).dropna(subset=FEATURES + ["label"])
X, y = df[FEATURES], df["label"]

Xtr, Xva, ytr, yva = train_test_split(
    X, y, test_size=0.2, stratify=y, random_state=42
)

train = lgb.Dataset(Xtr, label=ytr)
valid = lgb.Dataset(Xva, label=yva)

params = dict(
    objective="binary",
    metric="auc",
    learning_rate=0.03,
    num_leaves=31,
    min_data_in_leaf=30,
    feature_fraction=0.8,
    bagging_fraction=0.8,
    bagging_freq=5,
    verbosity=-1,
)

model = lgb.train(
    params,
    train,
    num_boost_round=2000,
    valid_sets=[valid],
    early_stopping_rounds=100,
)

pred = model.predict(Xva)
print("AUC =", roc_auc_score(yva, pred))

joblib.dump(model, MODEL_PATH)
json.dump(FEATURES, open(FEATURE_PATH, "w"), indent=2)
