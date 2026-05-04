# ============================================================
# train_holdtime_lgbm.py
# HOLDTIME AI（回帰）
# ------------------------------------------------------------
# 残り保持秒数を予測
# ============================================================

import pandas as pd
import lightgbm as lgb
import joblib
from pathlib import Path

DATA_PATH = Path("AI/train/holdtime/holdtime_train.csv")
MODEL_PATH = Path("AI/model/holdtime_lgbm.pkl")

FEATURES = [
    "profit_rate",
    "drawdown_rate",
    "hold_seconds",
    "volume_speed",
    "volatility",
    "trend_strength",
]

TARGET = "remaining_hold_seconds"


def main():

    df = pd.read_csv(DATA_PATH).dropna(subset=FEATURES + [TARGET])

    X = df[FEATURES]
    y = df[TARGET]

    model = lgb.LGBMRegressor(
        objective="regression",
        n_estimators=400,
        learning_rate=0.05,
        max_depth=6,
        subsample=0.8,
        colsample_bytree=0.8,
        random_state=42,
        n_jobs=-1,
    )

    model.fit(X, y)

    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, MODEL_PATH)

    print(f"✅ HOLDTIME model saved: {MODEL_PATH}")


if __name__ == "__main__":
    main()
