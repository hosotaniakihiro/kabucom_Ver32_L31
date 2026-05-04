# ============================================================
# pj/AI/train/train_tonosama_lgbm.py
# ------------------------------------------------------------
# 殿様イナゴ専用 LightGBM 学習
# ENTRY 30秒以内勝利確率を予測
# ============================================================

import json
import joblib
import pandas as pd
import lightgbm as lgb
from sklearn.model_selection import train_test_split
from sklearn.metrics import roc_auc_score

# ============================================================
# 設定
# ============================================================
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

TARGET = "label"
RANDOM_STATE = 42


# ============================================================
# データ読み込み
# ============================================================
df = pd.read_csv(CSV_PATH)

df = df.dropna(subset=FEATURES + [TARGET])
df = df.reset_index(drop=True)

X = df[FEATURES]
y = df[TARGET]

# ============================================================
# Train / Valid split
# ============================================================
X_train, X_valid, y_train, y_valid = train_test_split(
    X,
    y,
    test_size=0.2,
    random_state=RANDOM_STATE,
    stratify=y,
)

# ============================================================
# LightGBM Dataset
# ============================================================
train_data = lgb.Dataset(X_train, label=y_train)
valid_data = lgb.Dataset(X_valid, label=y_valid)

# ============================================================
# パラメータ（殿様イナゴ最適）
# ============================================================
params = {
    "objective": "binary",
    "metric": "auc",
    "boosting_type": "gbdt",
    "learning_rate": 0.03,
    "num_leaves": 31,
    "max_depth": -1,
    "min_data_in_leaf": 30,
    "feature_fraction": 0.8,
    "bagging_fraction": 0.8,
    "bagging_freq": 5,
    "lambda_l1": 0.1,
    "lambda_l2": 0.1,
    "verbosity": -1,
    "seed": RANDOM_STATE,
}

# ============================================================
# 学習
# ============================================================
model = lgb.train(
    params,
    train_data,
    num_boost_round=2000,
    valid_sets=[train_data, valid_data],
    valid_names=["train", "valid"],
    early_stopping_rounds=100,
    verbose_eval=100,
)

# ============================================================
# 評価
# ============================================================
y_pred = model.predict(X_valid, num_iteration=model.best_iteration)
auc = roc_auc_score(y_valid, y_pred)

print("=" * 60)
print(f"TONOSAMA LGBM AUC = {auc:.4f}")
print("=" * 60)

# ============================================================
# 保存
# ============================================================
joblib.dump(model, MODEL_PATH)

with open(FEATURE_PATH, "w", encoding="utf-8") as f:
    json.dump(FEATURES, f, indent=2)

print(f"✅ model saved -> {MODEL_PATH}")
print(f"✅ features saved -> {FEATURE_PATH}")

# ============================================================
# 特徴量重要度
# ============================================================
imp = pd.DataFrame({
    "feature": FEATURES,
    "importance": model.feature_importance(importance_type="gain"),
}).sort_values("importance", ascending=False)

print("\n📊 Feature Importance")
print(imp)
