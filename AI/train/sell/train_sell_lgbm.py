# ============================================================
# SELL AI 学習（LightGBM）
# ------------------------------------------------------------
# ・SELL してよかったか？（YES / NO）を学習
# ・方式選択は含めない（sell_ai_boost 側の責務）
# ・推論側 sell_lgbm.py と完全対称
# ============================================================

from pathlib import Path
import pandas as pd
import lightgbm as lgb
import joblib
from sklearn.model_selection import train_test_split
from sklearn.metrics import roc_auc_score, precision_score

# ============================================================
# BASE DIR（実行位置非依存）
# ============================================================
BASE_DIR = Path(__file__).resolve().parents[2]

# ============================================================
# PATH
# ============================================================
DATA_PATH = BASE_DIR / "train" / "sell_train.csv"
MODEL_PATH = BASE_DIR / "model" / "sell_lgbm.pkl"

# ============================================================
# FEATURES / TARGET
# ※ 推論側 sell_lgbm.py と完全一致させること
# ============================================================
FEATURES = [
    "profit_rate",
    "drawdown_rate",
    "hold_seconds",
    "volume_speed",
    "volatility",
    "trend_strength",
    "sell_mode_code",   # ← 追加
]


TARGET = "label"  # SELL して正解=1 / HOLD継続が正解=0

# ============================================================
# MAIN
# ============================================================
def main():

    if not DATA_PATH.exists():
        raise FileNotFoundError(f"❌ sell training csv not found: {DATA_PATH}")

    # --------------------------------------------------------
    # Load
    # --------------------------------------------------------
    df = pd.read_csv(DATA_PATH)

    # --------------------------------------------------------
    # Cleaning
    # --------------------------------------------------------
    df = df.dropna(subset=FEATURES + [TARGET])

    # 型安全化
    for c in FEATURES:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    df[TARGET] = df[TARGET].astype(int)
    df = df.dropna(subset=FEATURES)

    # 異常値除外（任意・安全側）
    df = df[
        (df["hold_seconds"] >= 0) &
        (df["hold_seconds"] < 60 * 60) &     # 1時間超は除外
        (df["volatility"] >= 0)
    ]

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
    # LightGBM Model
    # --------------------------------------------------------
    model = lgb.LGBMClassifier(
        objective="binary",
        boosting_type="gbdt",
        n_estimators=400,
        learning_rate=0.05,
        max_depth=6,
        num_leaves=31,
        subsample=0.8,
        colsample_bytree=0.8,
        min_child_samples=30,
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

    # 実運用想定：SELL は慎重（高閾値）
    THRESHOLD = 0.6
    pred_label = (prob >= THRESHOLD).astype(int)
    precision = precision_score(y_val, pred_label, zero_division=0)

    print("===================================")
    print("[SELL AI TRAIN]")
    print(f"AUC        = {auc:.4f}")
    print(f"Precision  = {precision:.4f} (thr={THRESHOLD})")
    print("===================================")

    # --------------------------------------------------------
    # Save
    # --------------------------------------------------------
    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, MODEL_PATH)

    print(f"✅ model saved -> {MODEL_PATH}")


# ============================================================
# Entry point
# ============================================================
if __name__ == "__main__":
    main()
