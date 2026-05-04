# ============================================================
# train_lightgbm_entry_immediate_profit.py
# ============================================================

import pandas as pd
import lightgbm as lgb
import joblib
from sklearn.model_selection import train_test_split
from sklearn.metrics import roc_auc_score
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]
CSV_PATH = BASE_DIR / "ai_train_entry_immediate_profit.csv"
MODEL_PATH = BASE_DIR / "model_entry_immediate_profit.pkl"

FEATURES = [
    "volume_speed",
    "price_velocity",
    "spread",
    "distance_from_vwap",
    "breakout_strength",
    "orderbook_imbalance",
]

def main():
    df = pd.read_csv(CSV_PATH).dropna()

    X = df[FEATURES]
    y = df["label_profit_30s"]

    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.2, shuffle=True
    )

    model = lgb.LGBMClassifier(
        objective="binary",
        n_estimators=300,
        learning_rate=0.03,
        num_leaves=31,
        subsample=0.8,
        colsample_bytree=0.8,
        random_state=42,
    )

    model.fit(X_train, y_train)

    pred = model.predict_proba(X_test)[:, 1]
    auc = roc_auc_score(y_test, pred)

    print(f"📈 AUC = {auc:.4f}")

    joblib.dump(model, MODEL_PATH)
    print(f"✅ model saved: {MODEL_PATH}")

if __name__ == "__main__":
    main()
