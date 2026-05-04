# ============================================================
# AI/train/final/train_final_decision_lgbm.py
# FINAL_DECISION AI 学習（LightGBM / 3class）
# ------------------------------------------------------------
# ✔ GO / HOLD / DELAY の3クラス分類
# ✔ EXIT直前の状態のみ使用（未来情報なし）
# ✔ HOLDTIME / HORIZON 拡張に耐える設計
# ✔ final_decision_ai.py と完全互換
# ============================================================

import pandas as pd
import lightgbm as lgb
import joblib
from pathlib import Path
from sklearn.model_selection import train_test_split
from sklearn.metrics import classification_report, confusion_matrix

# ============================================================
# PATH
# ============================================================

DATA_PATH = Path("AI/train/final/final_train.csv")
MODEL_PATH = Path("AI/model/final_decision_lgbm.pkl")

# ============================================================
# FEATURES
# ※ 推論時に必ず取得できるもののみ
# ============================================================

FEATURES = [
    "profit_rate",
    "drawdown_rate",
    "hold_seconds",
    "volume_speed",
    "volatility",
    "trend_strength",
]

TARGET = "label"   # 0=DELAY / 1=HOLD / 2=GO

# ============================================================
# メイン
# ============================================================

def main():

    if not DATA_PATH.exists():
        raise FileNotFoundError(f"❌ train csv not found: {DATA_PATH}")

    df = pd.read_csv(DATA_PATH)

    # --------------------------------------------------------
    # 前処理
    # --------------------------------------------------------
    df = df.dropna(subset=FEATURES + [TARGET])

    for c in FEATURES:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    df[TARGET] = pd.to_numeric(df[TARGET], errors="coerce").astype(int)

    df = df.dropna(subset=FEATURES)

    X = df[FEATURES]
    y = df[TARGET]

    # --------------------------------------------------------
    # クラス重み（EXIT判断最優先）
    # --------------------------------------------------------
    class_weight = {
        0: 1.0,   # DELAY
        1: 1.5,   # HOLD
        2: 3.0,   # GO（EXIT判断は最重要）
    }

    # --------------------------------------------------------
    # Train / Validation
    # --------------------------------------------------------
    X_train, X_val, y_train, y_val = train_test_split(
        X,
        y,
        test_size=0.2,
        random_state=42,
        stratify=y,
    )

    # --------------------------------------------------------
    # LightGBM
    # --------------------------------------------------------
    model = lgb.LGBMClassifier(
        objective="multiclass",
        num_class=3,

        boosting_type="gbdt",
        n_estimators=700,
        learning_rate=0.05,

        max_depth=6,
        num_leaves=31,
        min_child_samples=30,

        subsample=0.8,
        colsample_bytree=0.8,

        class_weight=class_weight,
        random_state=42,
        n_jobs=-1,
    )

    model.fit(
        X_train,
        y_train,
        eval_set=[(X_val, y_val)],
        eval_metric="multi_logloss",
        verbose=False,
    )

    # --------------------------------------------------------
    # 評価
    # --------------------------------------------------------
    y_pred = model.predict(X_val)

    print("=" * 70)
    print("📊 FINAL_DECISION AI REPORT")
    print("=" * 70)
    print(classification_report(
        y_val,
        y_pred,
        target_names=["DELAY", "HOLD", "GO"],
        digits=4,
    ))

    print("🧩 CONFUSION MATRIX")
    print(confusion_matrix(y_val, y_pred))

    # --------------------------------------------------------
    # 保存
    # --------------------------------------------------------
    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, MODEL_PATH)

    print("=" * 70)
    print(f"✅ model saved -> {MODEL_PATH}")
    print("=" * 70)


# ============================================================
if __name__ == "__main__":
    main()
