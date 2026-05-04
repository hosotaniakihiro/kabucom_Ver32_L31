# ============================================================
# pj/ai/train_holdtime_cluster.py
# クラスタ別 holding 秒数 AI
# ============================================================

import pandas as pd
import lightgbm as lgb
import joblib
from pathlib import Path

DATA_PATH = Path("AI/train/tosama_train.csv")
MODEL_DIR = Path("AI/model/holdtime_cluster")

FEATURES = [
    "volume_speed",
    "fast_ret",
    "rank_position",
    "price",
    "spread",
    "entry_second",
]

TARGET = "hold_seconds"


def train_cluster(cid, df):

    X = df[FEATURES]
    y = df[TARGET]

    model = lgb.LGBMRegressor(
        n_estimators=400,
        learning_rate=0.05,
        max_depth=6,
        num_leaves=31,
        subsample=0.8,
        colsample_bytree=0.8,
    )

    model.fit(X, y)
    path = MODEL_DIR / f"holdtime_cluster_{cid}.pkl"
    joblib.dump(model, path)
    print(f"saved {path}")


def main():
    df = pd.read_csv(DATA_PATH)
    MODEL_DIR.mkdir(parents=True, exist_ok=True)

    for cid in sorted(df["cluster_id"].unique()):
        cdf = df[df["cluster_id"] == cid]
        if len(cdf) < 50:
            continue
        train_cluster(cid, cdf)


if __name__ == "__main__":
    main()
