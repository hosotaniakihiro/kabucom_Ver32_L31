# ============================================================
# AI/train_lightgbm_mode_selector.py
# ------------------------------------------------------------
# ✔ BREAKOUT / PULLBACK 別に LightGBM 学習
# ✔ model_breakout.pkl / model_pullback.pkl を生成
# ============================================================

import pandas as pd
import lightgbm as lgb
import joblib
from pathlib import Path
from sklearn.model_selection import train_test_split
from sklearn.metrics import roc_auc_score

CSV_PATH = Path("AI/train_entry_mode_selector.csv")
MODEL_DIR = Path("AI")

COMMON_FEATURES = [
    "volume_speed",
    "price_velocity",
    "spread",
    "distance_from_vwap",
    "orderbook_imbalance",
    "trend_strength",
]

BREAKOUT_FEATURES = [
    "high_break_distance",
    "range_compression",
    "recent_high_count",
]

PULLBACK_FEATURES = [
    "pullback_depth",
    "vwap_touch_count",
    "ma_support_strength",
]


# ============================================================
def train_one(df, features, model_name):
    X = df[features]
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

    print(f"📈 {model_name} AUC = {auc:.4f}")

    path = MODEL_DIR / f"{model_name}.pkl"
    joblib.dump(model, path)
    print(f"✅ model saved: {path}")


# ============================================================
def main():
    df = pd.read_csv(CSV_PATH)

    # -----------------------------
    # BREAKOUT
    # -----------------------------
    df_b = df[df["mode"] == "BREAKOUT"].dropna()
    train_one(
        df_b,
        COMMON_FEATURES + BREAKOUT_FEATURES,
        "model_breakout"
    )

    # -----------------------------
    # PULLBACK
    # -----------------------------
    df_p = df[df["mode"] == "PULLBACK"].dropna()
    train_one(
        df_p,
        COMMON_FEATURES + PULLBACK_FEATURES,
        "model_pullback"
    )


if __name__ == "__main__":
    main()
ze