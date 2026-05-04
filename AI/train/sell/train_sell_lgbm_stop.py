# ============================================================
# AI/train/sell/train_sell_lgbm_stop.py
# SELL STOP 専用 AI（LightGBM）
# ------------------------------------------------------------
# ・sell_train.csv から STOP のみ抽出
# ・損失拡大を防ぐため recall 重視
# ・exit_controller から predict_sell_stop() で使用
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
MODEL_PATH = Path("AI/model/sell_lgbm_stop.pkl")

# ============================================================
# 特徴量定義（STOP 専用）
# ============================================================
FEATURES = [
    "drawdown_rate",   # 含み損率（最重要）
    "hold_seconds",    # 保持時間
    "volume_speed",    # 出来高速度
    "volatility",      # ボラティリティ
]

TARGET = "label"      # 1 = SELL 正解 / 0 = HOLD 継続が正解

# ============================================================
# MAIN
# ============================================================
def main():

    if not DATA_PATH.exists():
        raise FileNotFoundError(f"train data not found: {DATA_PATH}")

    # --------------------------------------------------------
    # Load
    # --------------------------------------------------------
    df = pd.read_csv(DATA_PATH)

    # STOP のみ抽出
    df = df[df["sell_mode_code"] == 1]

    if df.empty:
        raise RuntimeError("STOP データが存在しません")

    # --------------------------------------------------------
    # 前処理
    # --------------------------------------------------------
    df = df.dropna(subset=FEATURES + [TARGET])

    # 型安全化
    for c in FEATURES:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    df[TARGET] = df[TARGET].astype(int)
    df = df.dropna(subset=FEATURES)

    X = df[FEATURES]
    y = df[TARGET]

    # --------------------------------------------------------
    # Train / Validation split
    # --------------------------------------------------------
    X_train, X_val, y_train, y_val = train_test_split(
        X,
        y,
        test_size=0.2,
        random_state=42,
        stratify=y,
    )

    # --------------------------------------------------------
    # LightGBM（STOP は recall 重視）
    # --------------------------------------------------------
    model = lgb.LGBMClassifier(
        objective="binary",
        boosting_type="gbdt",
        n_estimators=400,
        learning_rate=0.05,
        max_depth=5,
        num_leaves=31,
        subsample=0.8,
        colsample_bytree=0.8,
        min_child_samples=20,
        class_weight="balanced",
        random_state=42,
        n_jobs=-1,
    )

    model.fit(
        X_train,
        y_train,
        eval_set=[(X_val, y_val)],
        eval_metric="auc",
        verbose=False,
    )

    # --------------------------------------------------------
    # Evaluation
    # --------------------------------------------------------
    prob = model.predict_proba(X_val)[:, 1]
    auc = roc_auc_score(y_val, prob)

    print("=" * 60)
    print("[SELL STOP AI]")
    print(f"AUC = {auc:.4f}")
    print("-" * 60)

    # threshold = 0.45（実運用想定）
    pred_label = (prob >= 0.45).astype(int)

    print(
        classification_report(
            y_val,
            pred_label,
            digits=4,
            zero_division=0,
        )
    )
    print("=" * 60)

    # --------------------------------------------------------
    # Save
    # --------------------------------------------------------
    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, MODEL_PATH)

    print(f"✅ model saved -> {MODEL_PATH}")


# ============================================================
# Entry Point
# ============================================================
if __name__ == "__main__":
    main()
