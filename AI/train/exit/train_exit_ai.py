# ============================================================
# AI/train/exit/train_exit_ai.py
# Ver1.1.0-FINAL-EXIT-AI-TRAINER
# ------------------------------------------------------------
# ✔ EXIT AI 学習（3クラス分類）
# ✔ build_exit_training_data.py 出力を入力
# ✔ LightGBM 使用（高速・高精度）
# ✔ クラス不均衡対策込み
# ✔ モデル / メタ / 評価結果 保存
# ============================================================

from __future__ import annotations

import json
from pathlib import Path
from typing import List, Tuple

import numpy as np
import pandas as pd
import lightgbm as lgb

from sklearn.model_selection import train_test_split
from sklearn.metrics import classification_report, confusion_matrix
from sklearn.utils.class_weight import compute_class_weight

from config.paths import get_path


# ============================================================
# パス設定
# ============================================================

DATA_DIR: Path = get_path("ai_train_exit")
DATA_FILE: Path = DATA_DIR / "exit_training_data.csv"

MODEL_DIR: Path = get_path("ai_model_exit")
MODEL_DIR.mkdir(parents=True, exist_ok=True)

MODEL_FILE: Path = MODEL_DIR / "exit_ai_lgbm.txt"
META_FILE: Path = MODEL_DIR / "exit_ai_meta.json"
REPORT_FILE: Path = MODEL_DIR / "exit_ai_train_report.json"


# ============================================================
# 設定
# ============================================================

SEED = 42
LABEL_COL = "label"

FEATURE_COLS: List[str] = [
    # 価格・リスク
    "atr_1min",
    "mfe_pct",
    "mae_pct",
    "mfe_atr",
    "mae_atr",

    # 時間
    "holding_seconds",
    "hold_sec_log",

    # 成果
    "pnl_pct",
    "miss_pct",

    # 市場環境
    "index_shock",
]

LGB_PARAMS = {
    "objective": "multiclass",
    "num_class": 3,                  # -1 / 0 / 1 → 0 / 1 / 2
    "metric": "multi_logloss",
    "learning_rate": 0.05,
    "num_leaves": 31,
    "max_depth": -1,
    "min_data_in_leaf": 50,
    "feature_fraction": 0.9,
    "bagging_fraction": 0.8,
    "bagging_freq": 5,
    "seed": SEED,
    "verbosity": -1,
}

NUM_BOOST_ROUND = 500
EARLY_STOPPING = 50
TEST_SIZE = 0.25


# ============================================================
# ロード
# ============================================================

def load_training_data() -> pd.DataFrame:
    if not DATA_FILE.exists():
        raise FileNotFoundError(f"❌ Training data not found: {DATA_FILE}")

    df = pd.read_csv(DATA_FILE)

    if df.empty:
        raise RuntimeError("❌ Training data is empty")

    return df


# ============================================================
# データ準備
# ============================================================

def prepare_dataset(
    df: pd.DataFrame,
) -> Tuple[pd.DataFrame, pd.DataFrame, np.ndarray, np.ndarray]:
    """
    学習 / 検証データ生成
    """
    # 特徴量チェック
    missing = set(FEATURE_COLS) - set(df.columns)
    if missing:
        raise RuntimeError(f"❌ Missing feature columns: {missing}")

    X = df[FEATURE_COLS].copy()
    y_raw = df[LABEL_COL].astype(int)

    # -1,0,1 → 0,1,2
    y = y_raw + 1

    return train_test_split(
        X,
        y,
        test_size=TEST_SIZE,
        random_state=SEED,
        stratify=y,
    )


# ============================================================
# 学習
# ============================================================

def train_model(
    X_train: pd.DataFrame,
    X_val: pd.DataFrame,
    y_train: np.ndarray,
    y_val: np.ndarray,
):
    # クラス不均衡対策
    classes = np.unique(y_train)
    weights = compute_class_weight(
        class_weight="balanced",
        classes=classes,
        y=y_train,
    )
    class_weight = dict(zip(classes, weights))

    train_ds = lgb.Dataset(
        X_train,
        label=y_train,
        weight=[class_weight[y] for y in y_train],
    )

    val_ds = lgb.Dataset(
        X_val,
        label=y_val,
        reference=train_ds,
    )

    model = lgb.train(
        params=LGB_PARAMS,
        train_set=train_ds,
        valid_sets=[train_ds, val_ds],
        valid_names=["train", "valid"],
        num_boost_round=NUM_BOOST_ROUND,
        callbacks=[
            lgb.early_stopping(EARLY_STOPPING),
            lgb.log_evaluation(50),
        ],
    )

    return model


# ============================================================
# 評価
# ============================================================

def evaluate_model(model, X_val, y_val) -> dict:
    probs = model.predict(X_val)
    preds = probs.argmax(axis=1)

    report = classification_report(
        y_val,
        preds,
        target_names=["BAD_EXIT", "NEUTRAL_EXIT", "GOOD_EXIT"],
        output_dict=True,
    )

    cm = confusion_matrix(y_val, preds).tolist()

    print("\n========== CLASSIFICATION REPORT ==========\n")
    print(classification_report(
        y_val,
        preds,
        target_names=["BAD_EXIT", "NEUTRAL_EXIT", "GOOD_EXIT"],
    ))

    print("\n========== CONFUSION MATRIX ==========\n")
    print(confusion_matrix(y_val, preds))

    return {
        "classification_report": report,
        "confusion_matrix": cm,
    }


# ============================================================
# 保存
# ============================================================

def save_outputs(model, eval_result: dict):
    # モデル
    model.save_model(MODEL_FILE)

    # メタ情報（推論側で必須）
    meta = {
        "features": FEATURE_COLS,
        "label_mapping": {
            "-1": "BAD_EXIT",
            "0": "NEUTRAL_EXIT",
            "1": "GOOD_EXIT",
        },
        "model_type": "lightgbm",
        "num_class": 3,
        "seed": SEED,
    }

    with open(META_FILE, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)

    # 評価結果
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(eval_result, f, indent=2, ensure_ascii=False)

    print("\n✅ Model saved :", MODEL_FILE)
    print("✅ Meta  saved :", META_FILE)
    print("✅ Report saved:", REPORT_FILE)


# ============================================================
# メイン
# ============================================================

def main():
    print("📥 loading training data...")
    df = load_training_data()

    print("🔧 preparing dataset...")
    X_train, X_val, y_train, y_val = prepare_dataset(df)

    print("🚀 training EXIT AI...")
    model = train_model(X_train, X_val, y_train, y_val)

    print("📊 evaluating...")
    eval_result = evaluate_model(model, X_val, y_val)

    print("💾 saving outputs...")
    save_outputs(model, eval_result)

    print("\n🎉 EXIT AI training completed successfully")


# ============================================================
# entry point
# ============================================================

if __name__ == "__main__":
    main()
