import lightgbm as lgb
import pandas as pd
import numpy as np
from pathlib import Path

DATA = Path("AI/train_data_sell.csv")
MODEL = Path("AI/company_info_model_sell.txt")

FEATURES = [
    "summary_score",
    "ranking_score",
    "company_ai_prob",
    "ranking_ai_prob",
    "interval",
]
TARGET = "y"

def split_time(df, ratio=0.2):
    n = len(df)
    k = int(n*(1-ratio))
    return df.iloc[:k], df.iloc[k:]

df = pd.read_csv(DATA).fillna(0)
train, valid = split_time(df)

dtrain = lgb.Dataset(train[FEATURES], label=train[TARGET])
dvalid = lgb.Dataset(valid[FEATURES], label=valid[TARGET])

params = {
    "objective": "binary",
    "metric": "auc",
    "learning_rate": 0.05,
    "num_leaves": 31,
    "feature_fraction": 0.8,
    "bagging_fraction": 0.8,
    "bagging_freq": 5,
    "verbosity": -1,
    "seed": 42,
}

model = lgb.train(
    params,
    dtrain,
    num_boost_round=500,
    valid_sets=[dvalid],
    callbacks=[lgb.early_stopping(30), lgb.log_evaluation(50)],
)

model.save_model(str(MODEL))
print(f"💾 SELL model saved: {MODEL}")

if __name__ == "__main__":
    split_time()
