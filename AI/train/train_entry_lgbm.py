# ============================================================
# AI/train/train_entry_lgbm.py
# ------------------------------------------------------------
# ENTRY 成否分類AI（LightGBM）
# ============================================================

from pathlib import Path
import pandas as pd
import lightgbm as lgb
import joblib
from sklearn.model_selection import train_test_split
from sklearn.metrics import roc_auc_score

# ===================== PATH =====================

TRAIN_CSV = Path("AI/train/train_entry.csv")
MODEL_OUT = Path("AI/models/model_ENTRY.pkl")
MODEL_OUT.parent.mkdir(exist_ok=True)

# ===================== 特徴量 =====================

FEATURES = [
    "ret",
    "body",
    "range",
    "vol_ratio",
    "fast_ret",
]

TARGET = "y"

# ===================== LOAD =====================

df = pd.read_csv(TRAIN_CSV)

df = df.dropna(subset=FEATURES + [TARGET])

X = df[FEATURES]
y = df[TARGET]

# ===================== SPLIT =====================

X_train, X_val, y_train, y_val = train_test_split(
    X,
    y,
    test_size=0.2,
    shuffle=True,
    random_state=42,
)

# ===================== MODEL =====================

model = lgb.LGBMClassifier(
    objective="binary",
    num_leaves=64,
    learning_rate=0.05,
    n_estimators=300,
    subsample=0.8,
    colsample_bytree=0.8,
    random_state=42,
    n_jobs=-1,
)

model.fit(X_train, y_train)

# ===================== EVAL =====================

proba = model.predict_proba(X_val)[:, 1]
auc = roc_auc_score(y_val, proba)

print(f"✅ ENTRY AI AUC = {auc:.4f}")

# ===================== SAVE =====================

joblib.dump(model, MODEL_OUT)
print(f"💾 saved {MODEL_OUT}")
