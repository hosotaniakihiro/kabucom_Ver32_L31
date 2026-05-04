# ============================================================
# AI/train/sell/train_sell_lgbm_tp.py
# SELL TAKE_PROFIT 専用 AI（LightGBM）
# ------------------------------------------------------------
# ・利確タイミング最適化
# ・precision 重視
# ============================================================

import pandas as pd
import lightgbm as lgb
import joblib
from pathlib import Path
from sklearn.model_selection import train_test_split
from sklearn.metrics import roc_auc_score, classification_report

# ============================================================
# PATH
# ============================================================
DATA_PATH = Path("AI/train/sell/sell_train.csv")
MODEL_PATH = Path("AI/model/sell_lgbm_tp.pkl")

# ============================================================
# 特徴量（利確特化）
# ============================================================
FEATURES = [
    "profit_rate",       # 含み益率（最重要）
    "hold_seconds",      # 保持時間
    "trend_strength",    # トレンド
    "volatility",        # ボラ
]

TARGET = "label"

# ============================================================
def main():

    if not DATA_PATH.exists():
        raise FileNotFoundError(DATA_PATH)

    df = pd.read_csv(DATA_PATH)

    # TAKE_PROFIT のみ
    df = df[df["sell_mode_code"] == 0]
    if df.empty:
        raise RuntimeError("TAKE_PROFIT データがありません")

    df = df.dropna(subset=FEATURES + [TARGET])

    for c in FEATURES:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    df[TARGET] = df[TARGET].astype(int)
    df = df.dropna(subset=FEATURES)

    X = df[FEATURES]
    y = df[TARGET]

    X_tr, X_va, y_tr, y_va = train_test_split(
        X, y, test_size=0.2, stratify=y, random_state=42
    )

    model = lgb.LGBMClassifier(
        objective="binary",
        n_estimators=400,
        learning_rate=0.05,
        max_depth=6,
        num_leaves=31,
        subsample=0.8,
        colsample_bytree=0.8,
        class_weight="balanced",
        random_state=42,
        n_jobs=-1,
    )

    model.fit(
        X_tr,
        y_tr,
        eval_set=[(X_va, y_va)],
        eval_metric="auc",
        verbose=False,
    )

    prob = model.predict_proba(X_va)[:, 1]
    auc = roc_auc_score(y_va, prob)

    print("=" * 60)
    print("[SELL TAKE_PROFIT AI]")
    print(f"AUC = {auc:.4f}")
    print("-" * 60)

    # 運用想定 threshold = 0.60
    pred = (prob >= 0.60).astype(int)
    print(classification_report(y_va, pred, digits=4, zero_division=0))
    print("=" * 60)

    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, MODEL_PATH)

    print(f"✅ model saved -> {MODEL_PATH}")


if __name__ == "__main__":
    main()
