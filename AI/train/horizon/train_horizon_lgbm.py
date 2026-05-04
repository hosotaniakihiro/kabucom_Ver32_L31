# ============================================================
# train_horizon_lgbm.py
# horizon EXIT 判定AI（30/60/120秒 統合）
# ============================================================

import pandas as pd
import lightgbm as lgb
import joblib
from pathlib import Path

DATA = Path("AI/train/horizon/horizon_train.csv")
MODEL = Path("AI/model/horizon_lgbm.pkl")

FEATURES = ["profit_rate"]
TARGETS = ["label_30", "label_60", "label_120"]

def main():
    df = pd.read_csv(DATA).dropna()

    X = df[FEATURES]
    y = df[TARGETS]

    model = lgb.LGBMClassifier(
        objective="binary",
        n_estimators=300,
        learning_rate=0.05,
        num_leaves=31,
        random_state=42,
    )

    # Multi-output 用に 3モデルまとめて学習
    model.fit(X, y)

    MODEL.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, MODEL)
    print(f"✅ horizon model saved: {MODEL}")

if __name__ == "__main__":
    main()
